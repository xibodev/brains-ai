"""File-backed registry for gated executor sessions.

Why a file store (not just the DB): an exec session is a *live process* whose
transcript streams over time, started either from the CLI or from the dashboard
web process. A small per-session directory under the brains state dir is the
simplest thing that lets any reader (the dashboard, another CLI) tail a running
session's output and see its status without a shared in-memory bus.

Layout: ``<state>/exec/<exec_id>/{meta.json, transcript.log, status}``.
The brains agent-session row (DB) still carries attribution + the audit trail;
this store only holds the live transcript + run metadata.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


def _state_dir() -> Path:
    base = os.environ.get("BRAINS_STATE_DIR") or os.path.join(Path.home(), ".brains")
    d = Path(base) / "exec"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class ExecMeta:
    exec_id: str
    tool: str
    model: str | None
    workspace: str
    prompt: str
    operator: str | None
    session_id: str | None
    created_at: float
    status: str = "running"  # running | done | failed | cancelled
    returncode: int | None = None
    pid: int | None = None
    ended_at: float | None = None


def _dir(exec_id: str) -> Path:
    return _state_dir() / exec_id


def create(
    tool: str,
    model: str | None,
    workspace: str,
    prompt: str,
    operator: str | None = None,
    session_id: str | None = None,
) -> ExecMeta:
    exec_id = f"exec_{uuid.uuid4().hex[:12]}"
    d = _dir(exec_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = ExecMeta(
        exec_id=exec_id,
        tool=tool,
        model=model,
        workspace=workspace,
        prompt=prompt,
        operator=operator,
        session_id=session_id,
        created_at=time.time(),
    )
    _write_meta(meta)
    (d / "transcript.log").write_text("", encoding="utf-8")
    return meta


def _write_meta(meta: ExecMeta) -> None:
    (_dir(meta.exec_id) / "meta.json").write_text(
        json.dumps(asdict(meta), indent=0), encoding="utf-8"
    )


def load(exec_id: str) -> ExecMeta | None:
    p = _dir(exec_id) / "meta.json"
    if not p.is_file():
        return None
    try:
        return ExecMeta(**json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def append_output(exec_id: str, text: str) -> None:
    log = _dir(exec_id) / "transcript.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(text)


def read_output(exec_id: str, offset: int = 0, max_bytes: int = 200_000) -> tuple[str, int]:
    """Return ``(new_text, new_offset)`` from the transcript starting at ``offset``."""
    log = _dir(exec_id) / "transcript.log"
    if not log.is_file():
        return "", offset
    data = log.read_bytes()
    chunk = data[offset : offset + max_bytes]
    return chunk.decode("utf-8", errors="replace"), offset + len(chunk)


def set_status(
    exec_id: str, status: str, returncode: int | None = None, pid: int | None = None
) -> None:
    meta = load(exec_id)
    if meta is None:
        return
    meta.status = status
    if returncode is not None:
        meta.returncode = returncode
    if pid is not None:
        meta.pid = pid
    if status in {"done", "failed", "cancelled"}:
        meta.ended_at = time.time()
    _write_meta(meta)


def list_sessions(limit: int = 50) -> list[dict]:
    out: list[dict] = []
    base = _state_dir()
    if not base.is_dir():
        return out
    dirs = sorted(base.glob("exec_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs[:limit]:
        meta = load(d.name)
        if meta is not None:
            out.append(asdict(meta))
    return out
