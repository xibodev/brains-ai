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

Status truthfulness: this is config-gated and audited. It is NOT yet routed
through the governed-action approval contract — treat ``mail_send`` as an
operator-trusted surface until that lands (documented, not implied).
"""

from __future__ import annotations

import smtplib
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
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(
                host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            ) as smtp:
                stage = "handshake"
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, _password())
                stage = "send"
                smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        raise MailerError(
            f"smtp delivery failed during {stage}: {type(exc).__name__}",
            delivery_uncertain=stage == "send",
        ) from exc
    except OSError as exc:
        raise MailerError(
            f"smtp delivery failed during {stage}: {type(exc).__name__}",
            delivery_uncertain=stage == "send",
        ) from exc
    except Exception as exc:
        raise MailerError(
            f"smtp delivery failed during {stage}: {type(exc).__name__}",
            delivery_uncertain=stage == "send",
        ) from exc

    if record_event:
        append_event(
            "email_sent",
            f"to {to.strip()}: {subject.strip()}",
            metadata={"to": to.strip(), "subject": subject.strip(), "host": host},
            session_id=session_id,
        )
    return {"sent": True, "to": to.strip(), "subject": subject.strip()}


def notify_ask(code: str, title: str, workspace_slug: str | None = None) -> dict[str, Any]:
    """Best-effort email copy of a filed ASK to the operator's inbox.

    Returns a status dict instead of raising: an email outage must never
    block or fail the ask itself (the durable row is authoritative).
    """
    to = settings.operator_notify_email
    result: dict[str, Any] = {"attempted": bool(to), "sent": False}
    if not to:
        return result
    subject = f"[brains ASK {code}] {title}"
    body = (
        f"ASK {code} needs your decision.\n\n"
        f"Workspace: {workspace_slug or 'unknown'}\n"
        f"Title: {title}\n\n"
        f"Resolve it in the console (/app inbox) or via:\n"
        f"  brains-ai decision-resolve --code {code} --chosen <answer>\n"
    )
    try:
        sent = send_email(to, subject, body)
        result["sent"] = bool(sent.get("sent"))
    except Exception as exc:  # noqa: BLE001 - courtesy copy never blocks
        result["error"] = type(exc).__name__
    return result


__all__ = ["MailerError", "mailer_status", "notify_ask", "send_email"]
