"""Supported local configuration contract for the modern operator console.

This module deliberately exposes a small positive manifest. Historical provider,
gateway, bridge, email, telemetry, and alternate-storage settings remain readable by
the compatibility loader but are neither named nor writable through this surface.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION


class ConfigurationError(ValueError):
    """A bounded, non-disclosing configuration failure."""


class ConfigurationConflict(ConfigurationError):
    """The caller attempted to replace a revision it did not read."""


_THREAD_LOCK = threading.Lock()
_EDITABLE: dict[str, tuple[str, str]] = {
    # The gateway may have multiple workers and the MCP service is a separate
    # process.  Persisted writes therefore require a supervised-stack restart;
    # mutating only the handling process would report false convergence.
    "service.rate_limit_per_minute": ("rate_limit_per_minute", "restart_required"),
    "sqlite.busy_timeout_ms": ("sqlite_busy_timeout_ms", "restart_required"),
    "sqlite.enforce_foreign_keys": ("sqlite_enforce_foreign_keys", "restart_required"),
}


def _overlay_path() -> Path:
    from brains.config import settings

    return Path(settings.runtime_overlay).expanduser().resolve()


def _read_overlay(path: Path) -> tuple[dict[str, Any], bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        raise ConfigurationError("supported configuration is unavailable") from exc
    try:
        payload = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError("supported configuration is malformed") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("supported configuration is malformed")
    version = payload.get("schema_version", RUNTIME_OVERLAY_SCHEMA_VERSION)
    if version != RUNTIME_OVERLAY_SCHEMA_VERSION:
        raise ConfigurationError("supported configuration version is incompatible")
    return dict(payload), raw


def _revision(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"<absent>").hexdigest()


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize read/compare/replace across gateway processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":  # pragma: win32 cover
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":  # pragma: win32 cover
            import msvcrt

            handle.seek(0)
            unlock_flag = msvcrt.LK_UNLCK  # type: ignore[attr-defined]
            with contextlib.suppress(OSError):
                msvcrt.locking(  # type: ignore[attr-defined]
                    handle.fileno(),
                    unlock_flag,
                    1,
                )
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_replace(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(temporary).unlink()


def _validated_updates(changes: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    if not changes:
        raise ConfigurationError("at least one supported configuration change is required")
    if any(key not in _EDITABLE for key in changes):
        raise ConfigurationError("request contains an unsupported configuration field")
    updates: dict[str, Any] = {}
    modes: set[str] = set()
    for public_key, value in changes.items():
        internal_key, mode = _EDITABLE[public_key]
        if public_key == "service.rate_limit_per_minute":
            if type(value) is not int or not 0 <= value <= 100_000:
                raise ConfigurationError("supported configuration value is invalid")
        elif public_key == "sqlite.busy_timeout_ms":
            if type(value) is not int or not 0 <= value <= 300_000:
                raise ConfigurationError("supported configuration value is invalid")
        elif type(value) is not bool:
            raise ConfigurationError("supported configuration value is invalid")
        updates[internal_key] = value
        modes.add(mode)
    return updates, modes


def _source(payload: Mapping[str, Any], internal_key: str) -> str:
    return "runtime_overlay" if internal_key in payload else "environment_or_default"


def configuration_summary() -> dict[str, Any]:
    """Return only supported, bounded, non-secret effective configuration."""

    from brains.config import load_settings
    from brains.service.common import read_service_config
    from brains.wire import status as wire_status

    path = _overlay_path()
    # The revision and effective editable values must describe one disk
    # snapshot.  Loading under the same cross-process lock used by writes
    # prevents a new revision from being paired with the handling process's
    # stale module singleton.
    with _THREAD_LOCK, _file_lock(path):
        payload, raw = _read_overlay(path)
        effective = load_settings()
    service = read_service_config()
    host = str(service["gateway_host"])
    binding = "loopback" if host in {"127.0.0.1", "::1", "localhost"} else "configured"
    try:
        wire_rows = wire_status(Path.home()).get("tools", [])
    except (OSError, ValueError, TypeError):
        wire_rows = []
    harnesses = [
        {
            "tool": str(row.get("tool", "")),
            "detected": bool(row.get("detected")),
            "mcp_wired": bool(row.get("mcp_wired")),
            "mcp_transport": (
                row.get("mcp_transport")
                if row.get("mcp_transport") in {"streamable-http", "stdio", "sse"}
                else None
            ),
            "rule_wired": bool(row.get("rule_wired")),
            "mailbox_notification_mode": (
                row.get("mailbox_notification_mode")
                if row.get("mailbox_notification_mode") in {"pull", "turn_boundary"}
                else "pull"
            ),
        }
        for row in wire_rows
        if row.get("tool") in {"copilot-cli", "claude-code", "codex", "opencode"}
    ]
    fields = [
        {
            "key": "service.authentication",
            "category": "service",
            "value": "disabled" if effective.allow_unauthenticated_api else "required",
            "editable": False,
            "apply_mode": "read_only",
            "source": "effective",
        },
        {
            "key": "service.binding",
            "category": "service",
            "value": binding,
            "editable": False,
            "apply_mode": "read_only",
            "source": "service_config",
        },
        {
            "key": "service.gateway_port",
            "category": "service",
            "value": int(service["gateway_port"]),
            "editable": False,
            "apply_mode": "restart_required",
            "source": "service_config",
        },
        {
            "key": "service.rate_limit_per_minute",
            "category": "service",
            "value": effective.rate_limit_per_minute,
            "editable": True,
            "apply_mode": "restart_required",
            "source": _source(payload, "rate_limit_per_minute"),
        },
        {
            "key": "mcp.transport",
            "category": "mcp",
            "value": "streamable-http",
            "editable": False,
            "apply_mode": "read_only",
            "source": "service_contract",
        },
        {
            "key": "mcp.port",
            "category": "mcp",
            "value": int(service["mcp_port"]),
            "editable": False,
            "apply_mode": "restart_required",
            "source": "service_config",
        },
        {
            "key": "sqlite.backend",
            "category": "sqlite",
            "value": "sqlite",
            "editable": False,
            "apply_mode": "read_only",
            "source": "required_core",
        },
        {
            "key": "sqlite.database",
            "category": "sqlite",
            "value": "configured",
            "editable": False,
            "apply_mode": "read_only",
            "source": "effective",
        },
        {
            "key": "sqlite.busy_timeout_ms",
            "category": "sqlite",
            "value": effective.sqlite_busy_timeout_ms,
            "editable": True,
            "apply_mode": "restart_required",
            "source": _source(payload, "sqlite_busy_timeout_ms"),
        },
        {
            "key": "sqlite.enforce_foreign_keys",
            "category": "sqlite",
            "value": effective.sqlite_enforce_foreign_keys,
            "editable": True,
            "apply_mode": "restart_required",
            "source": _source(payload, "sqlite_enforce_foreign_keys"),
        },
    ]
    return {
        "revision": _revision(raw),
        "fields": fields,
        "harnesses": harnesses,
        "redaction": "secret values and filesystem locations are omitted",
    }


def apply_configuration(
    changes: Mapping[str, Any], *, expected_revision: str, actor: str
) -> dict[str, Any]:
    """Validate, audit, atomically apply, and recover one supported patch."""

    from brains.audit import record_required
    from brains.config import Settings, settings
    from brains.storage.migrations import init_db

    updates, modes = _validated_updates(changes)
    if len(expected_revision) != 64:
        raise ConfigurationConflict("configuration revision is invalid")
    path = _overlay_path()
    init_db()
    with _THREAD_LOCK, _file_lock(path):
        payload, original = _read_overlay(path)
        if _revision(original) != expected_revision:
            record_required(
                actor=actor,
                action="config.core_update.conflict",
                payload={"fields": sorted(changes), "apply_modes": sorted(modes)},
            )
            raise ConfigurationConflict("configuration changed; reload before retrying")
        candidate = dict(payload)
        candidate["schema_version"] = RUNTIME_OVERLAY_SCHEMA_VERSION
        candidate.update(updates)
        # Validate the complete effective shape before touching the live file.
        try:
            Settings.model_validate({**settings.model_dump(), **updates})
        except Exception as exc:  # noqa: BLE001 - never echo validation input
            raise ConfigurationError("supported configuration value is invalid") from exc
        encoded = yaml.safe_dump(candidate, sort_keys=True).encode("utf-8")
        audit_payload = {"fields": sorted(changes), "apply_modes": sorted(modes)}
        attempt_id = record_required(
            actor=actor, action="config.core_update.attempted", payload=audit_payload
        )
        replaced = False
        try:
            _atomic_replace(path, encoded)
            replaced = True
            result_revision = _revision(encoded)
            outcome_id = record_required(
                actor=actor,
                action="config.core_update",
                payload={
                    **audit_payload,
                    "attempt_audit_id": attempt_id,
                    "revision": result_revision,
                },
            )
        except Exception as exc:
            rollback_ok = True
            if replaced:
                try:
                    _atomic_replace(path, original)
                except Exception:  # noqa: BLE001 - bounded result below
                    rollback_ok = False
            with contextlib.suppress(Exception):
                record_required(
                    actor=actor,
                    action="config.core_update.failed",
                    payload={
                        **audit_payload,
                        "attempt_audit_id": attempt_id,
                        "error_type": type(exc).__name__,
                        "rollback": "restored" if rollback_ok else "failed",
                    },
                )
            message = (
                "configuration apply failed and the previous revision was restored"
                if rollback_ok
                else "configuration apply failed and automatic recovery did not complete"
            )
            raise ConfigurationError(message) from exc
    mode = "restart_required"
    return {
        "ok": True,
        "revision": result_revision,
        "apply_mode": mode,
        "reload_applied": False,
        "restart_required": True,
        "audit_id": outcome_id,
        "changed_fields": sorted(changes),
    }


__all__ = [
    "ConfigurationConflict",
    "ConfigurationError",
    "apply_configuration",
    "configuration_summary",
]
