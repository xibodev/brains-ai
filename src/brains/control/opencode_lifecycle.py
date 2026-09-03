"""Proof-bound OpenCode lifecycle attachment."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from brains.config import brains_state_dir
from brains.control import durable_mailbox as mailbox_ctl
from brains.control.sessions import (
    finalize_session,
    heartbeat_session,
    start_session,
    sweep_stale_session_leases,
)
from brains.storage import db as db_module
from brains.storage.migrations import init_db
from brains.storage.models import Mailbox, MailboxAttachment, Workspace

OPENCODE_ADAPTER = "opencode"
OPENCODE_SUPPORTED_VERSION = "1.18.25"
_LOCK_WAIT_SECONDS = 5.0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lifecycle_root() -> Path:
    root = brains_state_dir() / "adapter-lifecycle"
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


@contextmanager
def _native_lock(native_id: str) -> Iterator[None]:
    """Serialize one native identity without putting it in a path or journal."""
    path = _lifecycle_root() / f"{_digest(native_id)}.lock"
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            try:
                owner_pid = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                owner_pid = 0
            from brains.control.sessions import _pid_alive

            if owner_pid <= 0 or not _pid_alive(owner_pid):
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError("adapter lifecycle is busy") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            path.unlink()


def _journal(event: str, native_id: str, session_id: str | None, result: str) -> None:
    """Append a body/key/path-free lifecycle observation using hashes only."""
    payload = {
        "event": event,
        "native_sha256": _digest(native_id),
        "session_sha256": _digest(session_id) if session_id else None,
        "result": result,
    }
    path = _lifecycle_root() / "opencode.jsonl"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def _existing_mailboxes(native_id: str) -> list[tuple[Mailbox, Workspace]]:
    init_db()
    with db_module.SessionLocal() as session:
        rows = (
            session.query(Mailbox, Workspace)
            .join(Workspace, Workspace.id == Mailbox.workspace_id)
            .filter(
                Mailbox.kind == "agent",
                Mailbox.tool == OPENCODE_ADAPTER,
                Mailbox.native_tool_session_id == native_id,
            )
            .all()
        )
        return [(mailbox, workspace) for mailbox, workspace in rows]


def _active_session_id(mailbox_id: int) -> str | None:
    with db_module.SessionLocal() as session:
        row = (
            session.query(MailboxAttachment)
            .filter(
                MailboxAttachment.mailbox_id == mailbox_id,
                MailboxAttachment.active_slot == 1,
            )
            .one_or_none()
        )
        return row.session_id if row is not None else None


def attach_opencode_session(workspace_path: str, native_tool_session_id: str) -> dict[str, Any]:
    """Attach or renew one authoritative OpenCode Session without exposing its proof."""
    native_id = mailbox_ctl.validate_native_tool_session_id(native_tool_session_id)
    with _native_lock(native_id):
        mailbox_ctl.reconcile_managed_mailbox_bindings()
        # A killed harness cannot send a final event. The next authoritative
        # turn first expires its silent predecessor, then either renews the
        # still-current incarnation or creates a proof-bound successor.
        sweep_stale_session_leases()
        matches = _existing_mailboxes(native_id)
        requested = Path(workspace_path).expanduser().resolve()
        if any(Path(workspace.path).resolve() != requested for _mailbox, workspace in matches):
            _journal("chat.message", native_id, None, "conflict")
            raise RuntimeError("adapter identity is unavailable")
        if len(matches) > 1:
            _journal("chat.message", native_id, None, "conflict")
            raise RuntimeError("adapter identity is unavailable")

        if matches:
            mailbox, workspace = matches[0]
            if mailbox.status != "active":
                _journal("chat.message", native_id, None, "revoked")
                raise RuntimeError("adapter identity is unavailable")
            binding_path = mailbox_ctl._managed_binding_path(workspace, OPENCODE_ADAPTER, native_id)
            try:
                binding_secret = mailbox_ctl.read_mailbox_binding_file(
                    binding_path, managed_only=True
                )
            except Exception as exc:
                _journal("chat.message", native_id, None, "proof-unavailable")
                raise RuntimeError("adapter identity is unavailable") from exc
            session_id = _active_session_id(mailbox.id)
            if session_id is not None:
                try:
                    heartbeat_session(
                        session_id,
                        tool=OPENCODE_ADAPTER,
                        native_tool_session_id=native_id,
                        mailbox_binding_secret=binding_secret,
                        mailbox_notification_mode="pull",
                    )
                    _journal("chat.message", native_id, session_id, "renewed")
                    return {"ok": True, "state": "renewed", "session_id": session_id}
                except Exception:
                    pass
            result = start_session(
                workspace_path,
                tool=OPENCODE_ADAPTER,
                reuse_existing=False,
                auto_link_predecessor=True,
                predecessor_session_id=session_id,
                native_tool_session_id=native_id,
                mailbox_binding_secret=binding_secret,
                mailbox_notification_mode="pull",
            )
            _journal("chat.message", native_id, result["session_id"], "recovered")
            return {"ok": True, "state": "recovered", "session_id": result["session_id"]}

        started = start_session(
            workspace_path, tool=OPENCODE_ADAPTER, reuse_existing=False, auto_link_predecessor=True
        )
        session_id = started["session_id"]
        try:
            mailbox_ctl.create_managed_agent_mailbox(
                workspace_path,
                OPENCODE_ADAPTER,
                native_id,
                session_id,
                notification_mode="pull",
            )
        except BaseException:
            finalize_session(session_id, state="failed", summary="adapter attachment failed")
            _journal("chat.message", native_id, session_id, "failed")
            raise RuntimeError("adapter identity is unavailable") from None
        _journal("chat.message", native_id, session_id, "attached")
        return {"ok": True, "state": "attached", "session_id": session_id}


def delete_opencode_session(workspace_path: str, native_tool_session_id: str) -> dict[str, Any]:
    """Best-effort terminal detach after a native OpenCode deletion event."""
    native_id = mailbox_ctl.validate_native_tool_session_id(native_tool_session_id)
    with _native_lock(native_id):
        mailbox_ctl.reconcile_managed_mailbox_bindings()
        matches = _existing_mailboxes(native_id)
        requested = Path(workspace_path).expanduser().resolve()
        if len(matches) != 1 or Path(matches[0][1].path).resolve() != requested:
            _journal("session.deleted", native_id, None, "conflict")
            raise RuntimeError("adapter identity is unavailable")
        mailbox, _workspace = matches[0]
        if mailbox.status != "active":
            _journal("session.deleted", native_id, None, "already-deleted")
            return {"ok": True, "state": "already-deleted"}
        session_id = _active_session_id(mailbox.id)
        if session_id is None:
            _journal("session.deleted", native_id, None, "unavailable")
            raise RuntimeError("adapter identity is unavailable")
        try:
            mailbox_ctl.revoke_managed_agent_mailbox_binding(
                workspace_path,
                OPENCODE_ADAPTER,
                native_id,
                session_id,
            )
            finalize_session(session_id, state="completed", summary="native session deleted")
        except Exception as exc:
            _journal("session.deleted", native_id, session_id, "failed")
            raise RuntimeError("adapter identity is unavailable") from exc
        _journal("session.deleted", native_id, session_id, "deleted")
        return {"ok": True, "state": "deleted"}


__all__ = [
    "OPENCODE_SUPPORTED_VERSION",
    "attach_opencode_session",
    "delete_opencode_session",
]
