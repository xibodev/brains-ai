"""Human-consented, one-way SMTP copies of durable operator mail."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, update

from brains.audit import AuditWriteError, append_in_session, required_effect
from brains.authz.principal import Principal
from brains.authz.resolver import resolve_local_principal
from brains.config import settings
from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.mailer import MailerError, send_email
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import (
    Mailbox,
    MailDelivery,
    MailMessage,
    MailSmtpOutbox,
    OperatorMailboxSetting,
)

COPY_MODES = frozenset({"disabled", "notification", "full_body"})
OPEN_OUTBOX_STATUSES = ("queued", "retry", "sending")
MAX_ATTEMPTS = 5
VERIFY_TTL_MINUTES = 15
MAX_VERIFY_ATTEMPTS = 5
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_RE = re.compile(r"^[0-9]{6}$")
_VERIFY_DOMAIN = b"brains-mailbox-smtp-verification-v1\0"


class MailboxSmtpError(RuntimeError):
    """SMTP-copy configuration or processing failed without exposing an address."""


class MailboxSmtpUnavailableError(MailboxSmtpError, LookupError):
    """The requested operator mailbox is absent or not owned by the caller."""


class MailboxSmtpValidationError(MailboxSmtpError, ValueError):
    """A destination, verification code, or copy mode is invalid."""


class _PermanentOutboxError(MailboxSmtpError):
    pass


def _principal_or_local(principal: Principal | None) -> Principal:
    resolved = principal or resolve_local_principal()
    if not resolved.is_operator or resolved.operator_id is None or not resolved.is_human_channel:
        raise MailboxSmtpUnavailableError("mailbox unavailable")
    return resolved


def _operator_mailbox(
    session,
    address: str,
    principal: Principal,
    *,
    lock: bool = False,
) -> Mailbox:
    requested = (address or "").strip()
    query = session.query(Mailbox).filter(Mailbox.address == requested)
    if lock:
        if session.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        else:
            session.execute(
                update(Mailbox).where(Mailbox.address == requested).values(status=Mailbox.status)
            )
    mailbox = query.one_or_none()
    if (
        mailbox is None
        or mailbox.kind != "operator"
        or mailbox.status != "active"
        or mailbox.owner_operator_id != principal.operator_id
    ):
        raise MailboxSmtpUnavailableError("mailbox unavailable")
    return mailbox


def _normalize_destination(destination: str) -> str:
    value = (destination or "").strip()
    if (
        value != destination
        or len(value) > 254
        or not _EMAIL_RE.fullmatch(value)
        or any(character in value for character in "\r\n")
    ):
        raise MailboxSmtpValidationError("destination must be one valid email address")
    local, domain = value.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _destination_hint(destination: str) -> str:
    local, domain = destination.rsplit("@", 1)
    labels = domain.split(".")
    masked_local = f"{local[0]}***" if local else "***"
    masked_domain = f"{labels[0][0]}***" if labels and labels[0] else "***"
    suffix = f".{labels[-1]}" if len(labels) > 1 else ""
    return f"{masked_local}@{masked_domain}{suffix}"


def _destination_ref(mailbox_id: int) -> str:
    return f"mailbox.smtp.{mailbox_id}.{uuid.uuid4().hex}"


def _verification_digest(reference: str, code: str, admin_key: str) -> str:
    key = hashlib.sha256(_VERIFY_DOMAIN + admin_key.encode("utf-8")).digest()
    return hmac.new(
        key,
        reference.encode("utf-8") + b"\0" + code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _setting_row(session, mailbox_id: int) -> OperatorMailboxSetting:
    setting = session.get(OperatorMailboxSetting, mailbox_id)
    if setting is None:
        setting = OperatorMailboxSetting(mailbox_id=mailbox_id, smtp_copy_mode="disabled")
        session.add(setting)
        session.flush()
    return setting


def _cancel_open_outbox(session, mailbox_id: int, *, reason: str) -> int:
    return int(
        session.query(MailSmtpOutbox)
        .filter(
            MailSmtpOutbox.recipient_mailbox_id == mailbox_id,
            MailSmtpOutbox.status.in_(("queued", "retry")),
        )
        .update(
            {
                "status": "cancelled",
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": None,
                "error_code": reason,
                "updated_at": utc_now(),
            },
            synchronize_session=False,
        )
    )


def _load_destination(session, reference: str, admin_key: str) -> dict[str, Any]:
    from brains.control.secure_settings import get_scoped_value_in_transaction

    raw = get_scoped_value_in_transaction(session, reference, admin_key=admin_key)
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError) as exc:
        raise MailboxSmtpError("mailbox SMTP destination is unavailable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("address"), str):
        raise MailboxSmtpError("mailbox SMTP destination is unavailable")
    data["address"] = _normalize_destination(data["address"])
    return data


def _status_in_transaction(
    session,
    mailbox: Mailbox,
    setting: OperatorMailboxSetting | None,
    *,
    admin_key: str,
) -> dict[str, Any]:
    hint = None
    destination_state = "unconfigured"
    if setting is not None and setting.smtp_destination_ref:
        try:
            hint = _destination_hint(
                _load_destination(session, setting.smtp_destination_ref, admin_key)["address"]
            )
            destination_state = (
                "verified" if setting.smtp_destination_verified_at is not None else "pending"
            )
        except MailboxSmtpError:
            destination_state = "unavailable"
    counts = {
        status: count
        for status, count in session.query(MailSmtpOutbox.status, func.count(MailSmtpOutbox.id))
        .filter(MailSmtpOutbox.recipient_mailbox_id == mailbox.id)
        .group_by(MailSmtpOutbox.status)
        .all()
    }
    return {
        "mailbox": mailbox.address,
        "destination_state": destination_state,
        "destination_hint": hint,
        "copy_mode": setting.smtp_copy_mode if setting is not None else "disabled",
        "verified_at": _iso(setting.smtp_destination_verified_at) if setting else None,
        "full_body_consented_at": _iso(setting.smtp_consented_at) if setting else None,
        "outbox": {
            "open": sum(counts.get(status, 0) for status in OPEN_OUTBOX_STATUSES),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "uncertain": counts.get("uncertain", 0),
            "cancelled": counts.get("cancelled", 0),
        },
    }


def smtp_copy_status(
    address: str,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Return one owned mailbox's redacted destination, consent, and queue state."""
    resolved = _principal_or_local(principal)
    from brains.api.admin_key import ensure_admin_key

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved)
        setting = session.get(OperatorMailboxSetting, mailbox.id)
        return _status_in_transaction(session, mailbox, setting, admin_key=admin_key)


def request_smtp_destination_verification(
    address: str,
    destination: str,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Store a pending encrypted destination and send a short-lived challenge."""
    resolved = _principal_or_local(principal)
    normalized = _normalize_destination(destination)
    code = f"{secrets.randbelow(1_000_000):06d}"
    from brains.api.admin_key import ensure_admin_key
    from brains.control.secure_settings import (
        clear_scoped_value_in_transaction,
        set_scoped_value_in_transaction,
    )

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved, lock=True)
        setting = _setting_row(session, mailbox.id)
        sending = (
            session.query(MailSmtpOutbox.id)
            .filter(
                MailSmtpOutbox.recipient_mailbox_id == mailbox.id,
                MailSmtpOutbox.status == "sending",
            )
            .first()
        )
        if sending is not None:
            raise MailboxSmtpValidationError(
                "destination cannot change while an SMTP copy is sending"
            )
        previous_reference = setting.smtp_destination_ref
        reference = _destination_ref(mailbox.id)
        expires_at = utc_now() + timedelta(minutes=VERIFY_TTL_MINUTES)
        set_scoped_value_in_transaction(
            session,
            reference,
            json.dumps(
                {
                    "address": normalized,
                    "verification_digest": _verification_digest(reference, code, admin_key),
                    "verification_expires_at": expires_at.isoformat(),
                    "verification_attempts": 0,
                },
                sort_keys=True,
            ),
            admin_key=admin_key,
        )
        setting.smtp_destination_ref = reference
        setting.smtp_destination_verified_at = None
        setting.smtp_copy_mode = "disabled"
        setting.smtp_consented_at = None
        setting.smtp_consented_by_operator_id = None
        setting.updated_at = utc_now()
        session.flush()
        _cancel_open_outbox(session, mailbox.id, reason="destination_changed")
        if previous_reference and previous_reference != reference:
            clear_scoped_value_in_transaction(session, previous_reference)
        session.commit()
        mailbox_id = mailbox.id

    sent = False
    try:
        with required_effect(
            actor=resolved.describe(),
            action="mailbox.smtp_verification",
            payload={"mailbox_id": mailbox_id},
        ):
            send_email(
                normalized,
                "Verify your Brains mailbox email",
                (
                    f"Your Brains mailbox verification code is {code}.\n\n"
                    f"It expires in {VERIFY_TTL_MINUTES} minutes. If you did not request "
                    "this, ignore this email.\n"
                ),
                message_id=f"<verify-{reference.rsplit('.', 1)[-1]}@brains.local>",
                record_event=False,
            )
            sent = True
    except Exception as exc:
        if sent and isinstance(exc, AuditWriteError):
            raise MailboxSmtpError("verification email outcome is uncertain") from exc
        with _db_module.SessionLocal() as session:
            failed_setting = session.get(OperatorMailboxSetting, mailbox_id)
            if failed_setting is not None and failed_setting.smtp_destination_ref == reference:
                clear_scoped_value_in_transaction(session, reference)
                failed_setting.smtp_destination_ref = None
                failed_setting.smtp_destination_verified_at = None
                failed_setting.smtp_copy_mode = "disabled"
                failed_setting.smtp_consented_at = None
                failed_setting.smtp_consented_by_operator_id = None
                failed_setting.updated_at = utc_now()
                session.flush()
                session.commit()
        raise MailboxSmtpError("verification email could not be sent") from exc
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved)
        current_setting = session.get(OperatorMailboxSetting, mailbox.id)
        if current_setting is None or current_setting.smtp_destination_ref != reference:
            result = _status_in_transaction(
                session,
                mailbox,
                current_setting,
                admin_key=admin_key,
            )
            result.update({"verification_sent": True, "superseded": True})
            return result
        result = _status_in_transaction(session, mailbox, current_setting, admin_key=admin_key)
    result.update({"verification_sent": True, "superseded": False})
    return result


def verify_smtp_destination(
    address: str,
    code: str,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Confirm a challenge and enable privacy-preserving notification copies."""
    resolved = _principal_or_local(principal)
    normalized_code = (code or "").strip()
    if not _CODE_RE.fullmatch(normalized_code):
        raise MailboxSmtpValidationError("verification code is invalid or expired")
    from brains.api.admin_key import ensure_admin_key
    from brains.control.secure_settings import (
        clear_scoped_value_in_transaction,
        set_scoped_value_in_transaction,
    )

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    failed = False
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved, lock=True)
        setting = session.get(OperatorMailboxSetting, mailbox.id)
        if setting is None or not setting.smtp_destination_ref:
            raise MailboxSmtpValidationError("verification code is invalid or expired")
        reference = setting.smtp_destination_ref
        data = _load_destination(session, reference, admin_key)
        expires_raw = data.get("verification_expires_at")
        digest = data.get("verification_digest")
        try:
            expires_at = datetime.fromisoformat(str(expires_raw))
        except ValueError:
            expires_at = datetime.min.replace(tzinfo=UTC)
        valid = (
            isinstance(digest, str)
            and _aware(expires_at) >= utc_now()
            and hmac.compare_digest(
                digest,
                _verification_digest(reference, normalized_code, admin_key),
            )
        )
        if not valid:
            attempts = int(data.get("verification_attempts", 0)) + 1
            if attempts >= MAX_VERIFY_ATTEMPTS or _aware(expires_at) < utc_now():
                clear_scoped_value_in_transaction(session, reference)
                setting.smtp_destination_ref = None
            else:
                data["verification_attempts"] = attempts
                set_scoped_value_in_transaction(
                    session,
                    reference,
                    json.dumps(data, sort_keys=True),
                    admin_key=admin_key,
                )
            setting.smtp_destination_verified_at = None
            setting.smtp_copy_mode = "disabled"
            setting.smtp_consented_at = None
            setting.smtp_consented_by_operator_id = None
            setting.updated_at = utc_now()
            session.flush()
            append_in_session(
                session,
                actor=resolved.describe(),
                action="mailbox.smtp_destination_rejected",
                payload={"mailbox_id": mailbox.id},
            )
            session.commit()
            failed = True
        else:
            set_scoped_value_in_transaction(
                session,
                reference,
                json.dumps({"address": data["address"]}, sort_keys=True),
                admin_key=admin_key,
            )
            setting.smtp_destination_verified_at = utc_now()
            setting.smtp_copy_mode = "notification"
            setting.smtp_consented_at = None
            setting.smtp_consented_by_operator_id = None
            setting.updated_at = utc_now()
            session.flush()
            append_in_session(
                session,
                actor=resolved.describe(),
                action="mailbox.smtp_destination_verified",
                payload={"mailbox_id": mailbox.id, "copy_mode": "notification"},
            )
            session.commit()
            result = _status_in_transaction(session, mailbox, setting, admin_key=admin_key)
    if failed:
        raise MailboxSmtpValidationError("verification code is invalid or expired")
    return result


def set_smtp_copy_mode(
    address: str,
    copy_mode: str,
    *,
    consent_full_body: bool = False,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Change future-copy policy; full body requires an explicit human consent flag."""
    resolved = _principal_or_local(principal)
    mode = (copy_mode or "").strip().lower()
    if mode not in COPY_MODES:
        raise MailboxSmtpValidationError("copy_mode must be disabled, notification, or full_body")
    if mode == "full_body" and not consent_full_body:
        raise MailboxSmtpValidationError("full_body mode requires explicit consent")
    from brains.api.admin_key import ensure_admin_key

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved, lock=True)
        setting = session.get(OperatorMailboxSetting, mailbox.id)
        if setting is None:
            raise MailboxSmtpValidationError("a verified destination is required")
        if mode != "disabled" and (
            setting.smtp_destination_ref is None or setting.smtp_destination_verified_at is None
        ):
            raise MailboxSmtpValidationError("a verified destination is required")
        mode_changed = setting.smtp_copy_mode != mode
        if mode_changed:
            sending = (
                session.query(MailSmtpOutbox.id)
                .filter(
                    MailSmtpOutbox.recipient_mailbox_id == mailbox.id,
                    MailSmtpOutbox.status == "sending",
                )
                .first()
            )
            if sending is not None:
                raise MailboxSmtpValidationError(
                    "copy policy cannot change while an SMTP copy is sending"
                )
            _cancel_open_outbox(session, mailbox.id, reason="copy_mode_changed")
        setting.smtp_copy_mode = mode
        if mode == "full_body":
            setting.smtp_consented_at = utc_now()
            setting.smtp_consented_by_operator_id = resolved.operator_id
        else:
            setting.smtp_consented_at = None
            setting.smtp_consented_by_operator_id = None
        setting.updated_at = utc_now()
        session.flush()
        append_in_session(
            session,
            actor=resolved.describe(),
            action="mailbox.smtp_mode_changed",
            payload={"mailbox_id": mailbox.id, "copy_mode": mode},
        )
        session.commit()
        return _status_in_transaction(session, mailbox, setting, admin_key=admin_key)


def clear_smtp_destination(
    address: str,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Revoke one destination and cancel all copies that have not begun sending."""
    resolved = _principal_or_local(principal)
    from brains.api.admin_key import ensure_admin_key
    from brains.control.secure_settings import clear_scoped_value_in_transaction

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox = _operator_mailbox(session, address, resolved, lock=True)
        setting = _setting_row(session, mailbox.id)
        sending = (
            session.query(MailSmtpOutbox.id)
            .filter(
                MailSmtpOutbox.recipient_mailbox_id == mailbox.id,
                MailSmtpOutbox.status == "sending",
            )
            .first()
        )
        if sending is not None:
            raise MailboxSmtpValidationError(
                "destination cannot be removed while an SMTP copy is sending"
            )
        if setting.smtp_destination_ref:
            clear_scoped_value_in_transaction(session, setting.smtp_destination_ref)
        _cancel_open_outbox(session, mailbox.id, reason="destination_revoked")
        setting.smtp_destination_ref = None
        setting.smtp_destination_verified_at = None
        setting.smtp_copy_mode = "disabled"
        setting.smtp_consented_at = None
        setting.smtp_consented_by_operator_id = None
        setting.updated_at = utc_now()
        session.flush()
        append_in_session(
            session,
            actor=resolved.describe(),
            action="mailbox.smtp_destination_cleared",
            payload={"mailbox_id": mailbox.id},
        )
        session.commit()
        return _status_in_transaction(session, mailbox, setting, admin_key=admin_key)


def _recover_expired_leases(session, now: datetime) -> int:
    rows = (
        session.query(MailSmtpOutbox)
        .filter(
            MailSmtpOutbox.status == "sending",
            MailSmtpOutbox.lease_expires_at <= now,
        )
        .all()
    )
    for row in rows:
        row.lease_owner = None
        row.lease_expires_at = None
        row.sent_at = None
        row.updated_at = now
        row.status = "uncertain"
        row.error_code = "lease_expired_after_send_claim"
        row.next_attempt_at = None
    return len(rows)


def _claim_outbox(now: datetime, worker_id: str) -> str | None:
    for _attempt in range(8):
        with _db_module.SessionLocal() as session:
            _recover_expired_leases(session, now)
            query = session.query(MailSmtpOutbox).filter(
                or_(
                    MailSmtpOutbox.status == "queued",
                    ((MailSmtpOutbox.status == "retry") & (MailSmtpOutbox.next_attempt_at <= now)),
                )
            )
            row = query.order_by(MailSmtpOutbox.id.asc()).first()
            if row is None:
                session.commit()
                return None
            if session.get_bind().dialect.name == "postgresql":
                session.query(Mailbox.id).filter(
                    Mailbox.id == row.recipient_mailbox_id
                ).with_for_update().one()
            else:
                session.execute(
                    update(Mailbox)
                    .where(Mailbox.id == row.recipient_mailbox_id)
                    .values(status=Mailbox.status)
                )
            expected = row.status
            lease_seconds = max(60, int(settings.smtp_timeout_seconds) + 30)
            updated = (
                session.query(MailSmtpOutbox)
                .filter(MailSmtpOutbox.id == row.id, MailSmtpOutbox.status == expected)
                .update(
                    {
                        "status": "sending",
                        "attempt": MailSmtpOutbox.attempt + 1,
                        "lease_owner": worker_id,
                        "lease_expires_at": now + timedelta(seconds=lease_seconds),
                        "next_attempt_at": None,
                        "error_code": None,
                        "updated_at": now,
                    },
                    synchronize_session=False,
                )
            )
            if not updated:
                session.rollback()
                continue
            session.commit()
            return row.outbox_id
    return None


def _cancel_claimed(outbox_id: str, worker_id: str, reason: str) -> None:
    with _db_module.SessionLocal() as session:
        session.query(MailSmtpOutbox).filter(
            MailSmtpOutbox.outbox_id == outbox_id,
            MailSmtpOutbox.status == "sending",
            MailSmtpOutbox.lease_owner == worker_id,
        ).update(
            {
                "status": "cancelled",
                "lease_owner": None,
                "lease_expires_at": None,
                "next_attempt_at": None,
                "error_code": reason,
                "updated_at": utc_now(),
            },
            synchronize_session=False,
        )
        session.commit()


def _render_claimed(
    outbox_id: str,
    worker_id: str,
    admin_key: str,
) -> tuple[int, str, str, str, str]:
    with _db_module.SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .filter(
                MailSmtpOutbox.outbox_id == outbox_id,
                MailSmtpOutbox.status == "sending",
                MailSmtpOutbox.lease_owner == worker_id,
            )
            .one_or_none()
        )
        if row is None:
            raise _PermanentOutboxError("outbox claim is unavailable")
        setting = session.get(OperatorMailboxSetting, row.recipient_mailbox_id)
        if (
            setting is None
            or setting.smtp_destination_ref != row.smtp_destination_ref
            or setting.smtp_destination_verified_at is None
            or setting.smtp_copy_mode != row.copy_mode
            or row.copy_mode not in {"notification", "full_body"}
            or (
                row.copy_mode == "full_body"
                and (
                    setting.smtp_consented_at is None
                    or setting.smtp_consented_by_operator_id is None
                )
            )
        ):
            raise _PermanentOutboxError("SMTP copy consent is no longer current")
        destination = _load_destination(session, row.smtp_destination_ref, admin_key)["address"]
        delivery = session.get(MailDelivery, row.delivery_id)
        message = session.get(MailMessage, delivery.message_id) if delivery else None
        if delivery is None or message is None:
            raise _PermanentOutboxError("durable mailbox message is unavailable")
        if row.copy_mode == "notification":
            subject = "New Brains mailbox message"
            body = (
                "New mail is waiting in your Brains mailbox.\n\n"
                "Open Brains to read it and reply inside Brains. This address does not "
                "accept replies.\n"
            )
        else:
            subject = "Brains mailbox message"
            body = (
                "This is a one-way copy from your Brains mailbox. Reply inside Brains.\n\n"
                f"Subject: {message.subject}\n\n{message.body or ''}"
            )
        return row.recipient_mailbox_id, row.copy_mode, destination, subject, body


def _backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(3600, 60 * (2 ** max(0, attempt - 1))))


def _settle_claim(
    outbox_id: str,
    worker_id: str,
    *,
    outcome: str,
    error_code: str | None = None,
    now: datetime,
) -> str:
    with _db_module.SessionLocal() as session:
        row = (
            session.query(MailSmtpOutbox)
            .filter(
                MailSmtpOutbox.outbox_id == outbox_id,
                MailSmtpOutbox.status == "sending",
                MailSmtpOutbox.lease_owner == worker_id,
            )
            .one_or_none()
        )
        if row is None:
            return "lost"
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = now
        if outcome == "sent":
            row.status = "sent"
            row.error_code = None
            row.next_attempt_at = None
            row.sent_at = now
        elif outcome == "uncertain":
            row.status = "uncertain"
            row.error_code = error_code or "audit_outcome_unrecorded"
            row.next_attempt_at = None
            row.sent_at = None
        elif row.attempt >= MAX_ATTEMPTS:
            row.status = "failed"
            row.error_code = error_code or "smtp_failure"
            row.next_attempt_at = None
            row.sent_at = None
        else:
            row.status = "retry"
            row.error_code = error_code or "smtp_failure"
            row.next_attempt_at = now + _backoff(row.attempt)
            row.sent_at = None
        status = row.status
        session.commit()
        return status


def process_smtp_outbox(
    *,
    limit: int = 10,
    worker_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim and process a bounded batch; local delivery is never mutated."""
    from brains.api.admin_key import ensure_admin_key

    admin_key, _ = ensure_admin_key(print_banner=False)
    init_db()
    current = _aware(now or utc_now())
    worker = worker_id or f"smtp-{uuid.uuid4().hex[:16]}"
    outcomes = {"sent": 0, "retry": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
    claimed = 0
    for _index in range(max(1, min(int(limit), 100))):
        outbox_id = _claim_outbox(current, worker)
        if outbox_id is None:
            break
        claimed += 1
        try:
            mailbox_id, copy_mode, destination, subject, body = _render_claimed(
                outbox_id, worker, admin_key
            )
        except _PermanentOutboxError:
            _cancel_claimed(outbox_id, worker, "consent_unavailable")
            outcomes["cancelled"] += 1
            continue
        except Exception:
            _cancel_claimed(outbox_id, worker, "destination_unavailable")
            outcomes["cancelled"] += 1
            continue

        sent = False
        try:
            with required_effect(
                actor="system",
                action="mailbox.smtp_copy",
                payload={
                    "outbox_id": outbox_id,
                    "mailbox_id": mailbox_id,
                    "copy_mode": copy_mode,
                },
            ):
                send_email(
                    destination,
                    subject,
                    body,
                    message_id=f"<{outbox_id}@brains.local>",
                    record_event=False,
                )
                sent = True
        except Exception as exc:
            if sent and isinstance(exc, AuditWriteError):
                status = _settle_claim(
                    outbox_id,
                    worker,
                    outcome="uncertain",
                    error_code="audit_outcome_unrecorded",
                    now=current,
                )
            elif isinstance(exc, MailerError) and exc.delivery_uncertain:
                status = _settle_claim(
                    outbox_id,
                    worker,
                    outcome="uncertain",
                    error_code="smtp_delivery_uncertain",
                    now=current,
                )
            else:
                status = _settle_claim(
                    outbox_id,
                    worker,
                    outcome="failed",
                    error_code=(
                        "mailer_unavailable" if isinstance(exc, MailerError) else "smtp_failure"
                    ),
                    now=current,
                )
            with contextlib.suppress(Exception):
                append_event(
                    f"mailbox_smtp_{status}",
                    f"durable mailbox SMTP copy {status}",
                    metadata={"outbox_id": outbox_id, "copy_mode": copy_mode},
                    renew_session=False,
                )
        else:
            status = _settle_claim(outbox_id, worker, outcome="sent", now=current)
        if status in outcomes:
            outcomes[status] += 1
    return {"claimed": claimed, **outcomes}


__all__ = [
    "COPY_MODES",
    "MailboxSmtpError",
    "MailboxSmtpUnavailableError",
    "MailboxSmtpValidationError",
    "clear_smtp_destination",
    "process_smtp_outbox",
    "request_smtp_destination_verification",
    "set_smtp_copy_mode",
    "smtp_copy_status",
    "verify_smtp_destination",
]
