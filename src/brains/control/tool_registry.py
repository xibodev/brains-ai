from __future__ import annotations

import shlex
import shutil

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RegisteredTool


def _is_available(cli_command: str) -> bool:
    try:
        parts = shlex.split(cli_command, posix=False)
    except ValueError:
        parts = cli_command.split()
    executable = parts[0].strip("\"'") if parts else ""
    return bool(executable and shutil.which(executable))


def _tool_to_dict(row: RegisteredTool, *, on_path_now: bool | None = None) -> dict:
    result = {
        "name": row.name,
        "display_name": row.display_name,
        "cli_command": row.cli_command,
        "spawn_args": row.spawn_args,
        "capabilities": row.capabilities,
        "installed_at": row.installed_at.isoformat(),
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
        "is_available": bool(row.is_available),
        "notes": row.notes,
    }
    if on_path_now is not None:
        result["on_path_now"] = on_path_now
    return result


def register_tool(
    name: str,
    display_name: str,
    cli_command: str,
    *,
    spawn_args: str = "",
    capabilities: str = "",
    notes: str = "",
    verify: bool = True,
) -> dict:
    now = utc_now()
    available = _is_available(cli_command) if verify else None
    init_db()
    with SessionLocal() as session:
        row = session.query(RegisteredTool).filter(RegisteredTool.name == name).one_or_none()
        if row is None:
            row = RegisteredTool(
                name=name,
                display_name=display_name,
                cli_command=cli_command,
                spawn_args=spawn_args or None,
                capabilities=capabilities or None,
                installed_at=now,
                last_verified_at=now if verify else None,
                is_available=1 if available else 0,
                notes=notes or None,
            )
            session.add(row)
        else:
            row.display_name = display_name
            row.cli_command = cli_command
            row.spawn_args = spawn_args or None
            row.capabilities = capabilities or None
            row.notes = notes or None
            if verify:
                row.last_verified_at = now
                row.is_available = 1 if available else 0
        session.commit()
        session.refresh(row)
        result = _tool_to_dict(row, on_path_now=available if verify else None)
    append_event(
        "tool_registered",
        f"{name}: {cli_command}",
        metadata={"name": name, "available": available},
    )
    return result


def list_registered_tools(verify_now: bool = False) -> list[dict]:
    """List tools, optionally persisting a fresh control-plane PATH probe.

    ``registered_tools`` is the local control-plane registry. Remote machine
    readiness lives on ``runtimes`` and must not be overwritten from this
    process's PATH.
    """
    init_db()
    with SessionLocal() as session:
        rows = session.query(RegisteredTool).order_by(RegisteredTool.name.asc()).all()
        availability: dict[str, bool] = {}
        if verify_now:
            now = utc_now()
            for row in rows:
                available = _is_available(row.cli_command)
                availability[row.name] = available
                row.last_verified_at = now
                row.is_available = 1 if available else 0
            session.commit()
        return [
            _tool_to_dict(row, on_path_now=availability.get(row.name) if verify_now else None)
            for row in rows
        ]


def verify_tool(name: str, session_id: str | None = None) -> dict:
    """Re-check ``name`` against ``PATH`` and record optional Session attribution."""
    now = utc_now()
    init_db()
    with SessionLocal() as session:
        row = session.query(RegisteredTool).filter(RegisteredTool.name == name).one_or_none()
        if row is None:
            raise ValueError(f"unknown registered tool: {name}")
        available = _is_available(row.cli_command)
        row.last_verified_at = now
        row.is_available = 1 if available else 0
        session.commit()
        result = _tool_to_dict(row, on_path_now=available)
    append_event(
        "tool_verified",
        f"{name}: {'available' if available else 'missing'}",
        session_id=session_id,
        metadata={"name": name, "available": available},
    )
    return result
