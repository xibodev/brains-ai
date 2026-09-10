"""Outbound email — SMTP sender with SES-by-configuration.

One generic SMTP client (stdlib only, no new deps). Amazon SES exposes an
SMTP endpoint, so pointing ``BRAINS_SMTP_HOST`` at it gives SES without any
SES-specific code; a native API extra can come later if ever needed.

Surfaces:

* :func:`send_email` — one plain-text mail. Audited via the events ledger
  (``email_sent``) with recipient + subject, never body or credentials.
* :func:`notify_ask` — best-effort operator notification when an ASK is
  filed. Never blocks or fails the ask: email is a courtesy copy of a
  durable row, not its carrier.

ASK notifications require explicit owner opt-in and use only the configured
owner address. SMTP acceptance is not proof of inbox delivery. The generic
sender is retained internally; it is not a supported arbitrary-recipient tool.
"""

from __future__ import annotations

import re
import smtplib
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from brains.config import settings
from brains.control.events import append_event


class MailerError(RuntimeError):
    """Raised when sending fails. Never embeds credentials."""

    def __init__(self, message: str, *, delivery_uncertain: bool = False) -> None:
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain


def _refresh_secure_settings() -> None:
    """Apply encrypted settings for one-shot CLI/stdio processes."""
    from brains.api.admin_key import ensure_admin_key

    ensure_admin_key(print_banner=False)


def _password() -> str:
    raw = settings.smtp_password or ""
    # Env-ref form ("${SECRET_NAME}") resolved like provider keys.
    if raw.startswith("${") and raw.endswith("}"):
        import os

        return os.environ.get(raw[2:-1], "")
    return raw


def mailer_status() -> dict[str, Any]:
    """Redacted configuration snapshot — booleans and host only."""
    _refresh_secure_settings()
    return {
        "enabled": bool(settings.smtp_host),
        "smtp_host": settings.smtp_host or None,
        "smtp_port": settings.smtp_port,
        "smtp_timeout_seconds": settings.smtp_timeout_seconds,
        "starttls": settings.smtp_use_starttls,
        "from": settings.smtp_from or None,
        "has_credentials": bool(settings.smtp_username),
        "operator_notify_email": settings.operator_notify_email or None,
    }


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    session_id: str | None = None,
    message_id: str | None = None,
    record_event: bool = True,
) -> dict[str, Any]:
    """Send one plain-text email through configured SMTP.

    Raises :class:`MailerError` when the mailer is unconfigured or the
    SMTP conversation fails. The failure message names the host and stage
    only — never the password.
    """
    if not to or "@" not in to:
        raise ValueError("to must be an email address")
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    _refresh_secure_settings()
    host = settings.smtp_host
    if not host:
        raise MailerError("mailer is disabled: set BRAINS_SMTP_HOST (+ port/user/password/from)")
    from_addr = settings.smtp_from or settings.smtp_username or "brains@localhost"

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to.strip()
    msg["Subject"] = subject.strip()
    msg["Message-ID"] = message_id or make_msgid(domain="brains.local")
    msg.set_content(body)

    _smtp_send(msg, host)

    if record_event:
        append_event(
            "email_sent",
            f"to {to.strip()}: {subject.strip()}",
            metadata={"to": to.strip(), "subject": subject.strip(), "host": host},
            session_id=session_id,
        )
    return {"sent": True, "to": to.strip(), "subject": subject.strip()}


def _smtp_send(msg: EmailMessage, host: str) -> None:
    """One SMTP attempt; preserve known acceptance even if QUIT fails."""
    stage = "connect"
    try:
        if settings.smtp_use_starttls:
            with smtplib.SMTP(
                host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            ) as smtp:
                stage = "handshake"
                smtp.ehlo()
                smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, _password())
                stage = "send"
                refused = smtp.send_message(msg)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                stage = "accepted"
        else:
            with smtplib.SMTP(
                host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            ) as smtp:
                stage = "handshake"
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, _password())
                stage = "send"
                refused = smtp.send_message(msg)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                stage = "accepted"
    except Exception as exc:
        if stage == "accepted":
            return
        rejected = isinstance(
            exc, smtplib.SMTPRecipientsRefused | smtplib.SMTPSenderRefused | smtplib.SMTPDataError
        )
        raise MailerError(
            f"smtp delivery failed during {stage}: {type(exc).__name__}",
            delivery_uncertain=stage == "send" and not rejected,
        ) from exc


def notify_ask(
    code: str,
    title: str,
    workspace_slug: str | None = None,
    *,
    session_id: str | None = None,
    workspace_id: int | None = None,
) -> dict[str, Any]:
    """One opted-in courtesy notification after the ASK has been committed.

    No retries, historical outbox activation, or working-context export.
    ``sent`` is retained for compatibility and means SMTP acceptance only.
    Attribution is local ledger data, never email content.
    The emailed title is whitespace-normalized and capped at 200 Unicode
    scalars including an ellipsis (at most 800 UTF-8 bytes). CR/LF is rejected,
    not normalized away. The durable ASK's original title is untouched.
    """
    result: dict[str, Any] = {"status": "disabled", "attempted": False, "sent": False}
    safe_code = (
        code if isinstance(code, str) and re.fullmatch(r"ASK-[A-Za-z0-9]{1,28}", code) else None
    )

    def record(status: str) -> None:
        append_event(
            "decision_email_notification",
            f"ASK email notification: {status}",
            metadata={"code": safe_code, "status": status},
            session_id=session_id,
            workspace_id=workspace_id,
        )

    try:
        if settings.ask_email_notifications_enabled:
            result["status"] = "failed"
            if safe_code is None or "\r" in title or "\n" in title:
                raise ValueError("invalid notification content")
            short_title = " ".join(title.split())
            if len(short_title) > 200:
                short_title = short_title[:199] + "…"
                result["title_truncated"] = True
            # Reject unpaired surrogates rather than silently changing text.
            short_title.encode("utf-8")
            # Refresh before reading the recipient as well as SMTP configuration.
            _refresh_secure_settings()
            to = settings.operator_notify_email.strip()
            if not to or not settings.smtp_host:
                result["error"] = "incomplete_configuration"
            else:
                # A single addr-spec only: no lists, groups, or header injection.
                owner = Address(addr_spec=to)
                if not owner.username or not owner.domain:
                    raise ValueError("invalid owner address")
                msg = EmailMessage()
                msg["From"] = settings.smtp_from or settings.smtp_username or "brains@localhost"
                msg["To"] = str(owner)
                msg["Subject"] = f"[brains ASK {safe_code}] {short_title}"
                msg["Message-ID"] = make_msgid(domain="brains.local")
                msg.set_content(f"ASK {safe_code}: {short_title}\n\nhttp://127.0.0.1:8787/app\n")
                # No outward attempt if its local attribution cannot be recorded.
                record("attempted")
                result.update(status="attempted", attempted=True)
                _smtp_send(msg, settings.smtp_host)
                result.update(status="smtp_accepted", sent=True)
    except MailerError as exc:
        result["status"] = "uncertain" if exc.delivery_uncertain else "failed"
        result["error"] = "smtp_uncertain" if exc.delivery_uncertain else "smtp_failed"
    except Exception:  # noqa: BLE001 - courtesy copy must not fail the durable ASK
        result.update(status="failed", error="notification_failed")
    try:
        record(result["status"])
    except Exception:
        # A terminal ledger outage cannot erase an observed SMTP acknowledgement.
        result["audit_error"] = "outcome_record_failed"
    return result


__all__ = ["MailerError", "mailer_status", "notify_ask", "send_email"]
