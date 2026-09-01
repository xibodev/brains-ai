"""Admin-key encrypted local configuration.

The Brains database stores only AES-256-GCM ciphertext. A per-row key is
derived with Scrypt from the current admin key plus a random salt; the setting
name is authenticated as AAD. Environment variables remain the highest-
precedence runtime source, allowing service-managed overrides without DB
mutation.

Admin-key rotation calls :func:`rekey_all` before replacing the key file. A
failed re-key refuses rotation, so ciphertext is never orphaned silently.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import SecureSetting

FORMAT_VERSION = 1
SECRET_NAMES = frozenset(
    {
        "smtp_username",
        "smtp_password",
        "BRAINS_GITHUB_COPILOT_OAUTH_TOKEN",
        "BRAINS_GITHUB_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "BRAINS_TELEGRAM_BOT_TOKEN",
        "BRAINS_TELEGRAM_CHAT_ID",
        "BRAINS_SLACK_BOT_TOKEN",
        "BRAINS_SLACK_CHANNEL",
        "BRAINS_WHATSAPP_TOKEN",
        "BRAINS_WHATSAPP_PHONE_ID",
        "BRAINS_WHATSAPP_RECIPIENT",
        "BRAINS_WHATSAPP_WEB_URL",
        "BRAINS_WHATSAPP_WEB_TOKEN",
    }
)
PLAIN_NAMES = frozenset(
    {
        "smtp_host",
        "smtp_port",
        "smtp_from",
        "smtp_use_starttls",
        "smtp_timeout_seconds",
        "operator_notify_email",
    }
)
ALLOWED_NAMES = SECRET_NAMES | PLAIN_NAMES
ENV_SETTING_NAMES = frozenset(name for name in SECRET_NAMES if name.isupper())
SETTING_SECRET_NAMES = SECRET_NAMES - ENV_SETTING_NAMES
ENV_TO_SETTING = {
    "BRAINS_GITHUB_COPILOT_OAUTH_TOKEN": "github_copilot_oauth_token",
    "BRAINS_GITHUB_WEBHOOK_SECRET": "github_webhook_secret",
    "OPENAI_API_KEY": "openai_compatible_api_key",
}
_INJECTED_ENV: set[str] = set()
_SCOPED_SETTING_RE = re.compile(r"^mailbox\.smtp\.[1-9][0-9]*\.[0-9a-f]{32}$")


class SecureSettingError(RuntimeError):
    """Encrypted configuration cannot be read or changed safely."""


def _derive_key(admin_key: str, salt: bytes) -> bytes:
    if not admin_key:
        raise SecureSettingError("admin key is required to decrypt secure settings")
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(admin_key.encode("utf-8"))


def _encrypt(name: str, value: str, admin_key: str) -> tuple[bytes, bytes, bytes]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ciphertext = AESGCM(_derive_key(admin_key, salt)).encrypt(
        nonce, value.encode("utf-8"), name.encode("utf-8")
    )
    return ciphertext, nonce, salt


def _decrypt(row: SecureSetting, admin_key: str) -> str:
    if row.version != FORMAT_VERSION:
        raise SecureSettingError(
            f"secure setting {row.name!r} uses unsupported format version {row.version}"
        )
    try:
        plaintext = AESGCM(_derive_key(admin_key, bytes(row.salt))).decrypt(
            bytes(row.nonce), bytes(row.ciphertext), row.name.encode("utf-8")
        )
    except InvalidTag as exc:
        raise SecureSettingError(
            f"secure setting {row.name!r} could not be decrypted with the current admin key"
        ) from exc
    return plaintext.decode("utf-8")


def _validate_name(name: str) -> str:
    normalized = (name or "").strip()
    if normalized not in ALLOWED_NAMES:
        raise ValueError(f"unsupported encrypted setting: {normalized!r}")
    return normalized


def _validate_scoped_name(name: str) -> str:
    normalized = (name or "").strip()
    if not _SCOPED_SETTING_RE.fullmatch(normalized):
        raise ValueError("unsupported scoped encrypted setting")
    return normalized


def _set_encrypted_row(session, name: str, value: str, admin_key: str) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError("secure setting value must be a non-empty string")
    ciphertext, nonce, salt = _encrypt(name, value, admin_key)
    row = session.get(SecureSetting, name)
    if row is None:
        row = SecureSetting(name=name)
        session.add(row)
    row.ciphertext = ciphertext
    row.nonce = nonce
    row.salt = salt
    row.version = FORMAT_VERSION
    row.updated_at = datetime.now(UTC)


def set_value(name: str, value: str, *, admin_key: str) -> dict[str, Any]:
    normalized = _validate_name(name)
    init_db()
    with SessionLocal() as session:
        _set_encrypted_row(session, normalized, value, admin_key)
        session.commit()
    return {"name": normalized, "set": True}


def clear_value(name: str) -> dict[str, Any]:
    normalized = _validate_name(name)
    init_db()
    with SessionLocal() as session:
        removed = (
            session.query(SecureSetting)
            .filter(SecureSetting.name == normalized)
            .delete(synchronize_session=False)
        )
        session.commit()
    return {"name": normalized, "set": False, "removed": bool(removed)}


def get_value(name: str, *, admin_key: str) -> str | None:
    normalized = _validate_name(name)
    init_db()
    with SessionLocal() as session:
        row = session.get(SecureSetting, normalized)
        return _decrypt(row, admin_key) if row else None


def set_scoped_value_in_transaction(
    session,
    name: str,
    value: str,
    *,
    admin_key: str,
) -> None:
    """Write one mailbox-scoped encrypted value in the caller's transaction."""
    _set_encrypted_row(session, _validate_scoped_name(name), value, admin_key)


def get_scoped_value_in_transaction(
    session,
    name: str,
    *,
    admin_key: str,
) -> str | None:
    """Decrypt one mailbox-scoped value without exposing generic dynamic names."""
    normalized = _validate_scoped_name(name)
    row = session.get(SecureSetting, normalized)
    return _decrypt(row, admin_key) if row else None


def clear_scoped_value_in_transaction(session, name: str) -> bool:
    """Delete one mailbox-scoped ciphertext row in the caller's transaction."""
    normalized = _validate_scoped_name(name)
    return bool(
        session.query(SecureSetting)
        .filter(SecureSetting.name == normalized)
        .delete(synchronize_session=False)
    )


def values(admin_key: str) -> dict[str, str]:
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(SecureSetting)
            .filter(SecureSetting.name.in_(ALLOWED_NAMES))
            .order_by(SecureSetting.name)
            .all()
        )
        return {row.name: _decrypt(row, admin_key) for row in rows}


def status(admin_key: str) -> dict[str, Any]:
    init_db()
    with SessionLocal() as session:
        names = {
            row[0]
            for row in session.query(SecureSetting.name)
            .filter(SecureSetting.name.in_(ALLOWED_NAMES))
            .all()
        }
    return {
        "encrypted_store": "brains-db/aes-256-gcm+scrypt/admin-key",
        "settings": {
            name: {
                "set": name in names,
                "secret": name in SECRET_NAMES,
                "source": "encrypted" if name in names else "unset",
            }
            for name in sorted(ALLOWED_NAMES)
        },
    }


def source_for(name: str, encrypted_names: set[str]) -> str:
    env_name = next(
        (env for env, field in ENV_TO_SETTING.items() if field == name),
        name if name in ENV_SETTING_NAMES else f"BRAINS_{name.upper()}",
    )
    if env_name in os.environ and env_name not in _INJECTED_ENV:
        return "environment"
    return "encrypted" if name in encrypted_names else "unset"


def apply_to_settings(settings_obj: Any, admin_key: str) -> None:
    """Overlay decrypted values without replacing explicit process env vars."""
    loaded = values(admin_key)
    cleared_fields: list[str] = []
    for name in list(_INJECTED_ENV):
        if name not in loaded:
            os.environ.pop(name, None)
            _INJECTED_ENV.discard(name)
            if name in ENV_TO_SETTING:
                cleared_fields.append(ENV_TO_SETTING[name])
    if cleared_fields:
        defaults = type(settings_obj)()
        for field in cleared_fields:
            object.__setattr__(settings_obj, field, getattr(defaults, field))
    for name in ENV_SETTING_NAMES:
        if name not in loaded:
            continue
        if name not in os.environ or name in _INJECTED_ENV:
            os.environ[name] = loaded[name]
            _INJECTED_ENV.add(name)
    for env_name, field in ENV_TO_SETTING.items():
        env_value = os.environ.get(env_name)
        if env_value:
            object.__setattr__(settings_obj, field, env_value)
    for field in sorted(ALLOWED_NAMES):
        if field in ENV_SETTING_NAMES:
            continue
        if f"BRAINS_{field.upper()}" in os.environ or field not in loaded:
            continue
        setting_value: Any = loaded[field]
        if field == "smtp_port":
            setting_value = int(setting_value)
        elif field == "smtp_timeout_seconds":
            setting_value = float(setting_value)
        elif field == "smtp_use_starttls":
            setting_value = setting_value.strip().lower() in {"1", "true", "yes", "on"}
        object.__setattr__(settings_obj, field, setting_value)


def rekey_all(old_key: str, new_key: str) -> int:
    """Atomically decrypt with ``old_key`` and re-encrypt with ``new_key``."""
    init_db()
    with SessionLocal() as session:
        rows = session.query(SecureSetting).order_by(SecureSetting.name).all()
        plaintext = [(row, _decrypt(row, old_key)) for row in rows]
        for row, value in plaintext:
            ciphertext, nonce, salt = _encrypt(row.name, value, new_key)
            row.ciphertext = ciphertext
            row.nonce = nonce
            row.salt = salt
            row.version = FORMAT_VERSION
            row.updated_at = datetime.now(UTC)
        session.commit()
        return len(rows)


def delete_orphaned_mailbox_smtp_settings() -> int:
    """Remove dynamic destination ciphertext no mailbox setting references."""
    init_db()
    from brains.storage.models import OperatorMailboxSetting

    with SessionLocal() as session:
        references = session.query(OperatorMailboxSetting.smtp_destination_ref).filter(
            OperatorMailboxSetting.smtp_destination_ref.isnot(None)
        )
        removed = (
            session.query(SecureSetting)
            .filter(
                SecureSetting.name.like("mailbox.smtp.%"),
                ~SecureSetting.name.in_(references),
            )
            .delete(synchronize_session=False)
        )
        session.commit()
        return int(removed)


__all__ = [
    "ALLOWED_NAMES",
    "PLAIN_NAMES",
    "SECRET_NAMES",
    "ENV_SETTING_NAMES",
    "ENV_TO_SETTING",
    "SETTING_SECRET_NAMES",
    "SecureSettingError",
    "apply_to_settings",
    "clear_value",
    "clear_scoped_value_in_transaction",
    "delete_orphaned_mailbox_smtp_settings",
    "get_scoped_value_in_transaction",
    "get_value",
    "rekey_all",
    "set_value",
    "set_scoped_value_in_transaction",
    "status",
    "source_for",
    "values",
]
