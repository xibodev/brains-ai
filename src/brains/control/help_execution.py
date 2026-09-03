"""On-demand ephemeral reviewers for durable peer-help requests.

Reviewers never receive the registered Workspace path. Brains copies only
Git-tracked content into a temporary snapshot, launches a provider in its
read-only headless mode there, verifies the source fingerprint afterwards, and
destroys the snapshot. The help request, Session, governed action and answer
remain durable; the sandbox does not.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult

from brains.control.common import normalize_path, utc_now
from brains.control.events import append_event
from brains.govern.redaction import redact_text
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    HelpRequest,
    HelpRequestExecution,
    Runtime,
    Workspace,
    WorkspaceAlias,
)

REVIEW_STATUSES = frozenset({"queued", "running", "answered", "failed", "cancelled"})
MAX_REVIEW_OUTPUT_BYTES = 100_000
MAX_PATCH_BYTES = 1_000_000
DEFAULT_REVIEW_TIMEOUT_SECONDS = 10 * 60
DEFAULT_REVIEW_LEASE_SECONDS = 15 * 60
MAX_REVIEW_ATTEMPTS = 3

_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReviewRun:
    answer: str
    evidence: str
    returncode: int
    source_unchanged: bool
    error_code: str | None = None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _review_timeout_seconds() -> int:
    try:
        raw = int(os.environ.get("BRAINS_HELP_REVIEW_TIMEOUT_SECONDS", "600"))
    except ValueError:
        raw = DEFAULT_REVIEW_TIMEOUT_SECONDS
    return max(30, min(raw, 60 * 60))


def _review_lease_seconds() -> int:
    try:
        raw = int(os.environ.get("BRAINS_HELP_REVIEW_LEASE_SECONDS", "900"))
    except ValueError:
        raw = DEFAULT_REVIEW_LEASE_SECONDS
    return max(_review_timeout_seconds() + 30, min(raw, 2 * 60 * 60))


def _git(source: Path, args: list[str], *, workspace_id: int) -> str:
    from brains.exec.guard import run

    result = run(
        ["git", "-C", str(source), *args],
        actor="brains-help-review",
        action="help.review.snapshot",
        workspace_id=workspace_id,
        workspace_path=str(source),
        cwd=str(source),
        capture_output=True,
        wait_for_approval=False,
    )
    if not result.allowed or result.returncode != 0:
        raise RuntimeError("tracked source could not be read")
    return result.stdout or ""


def _tracked_paths(source: Path, *, workspace_id: int) -> tuple[str, ...]:
    output = _git(source, ["ls-files", "-z", "--cached"], workspace_id=workspace_id)
    paths: list[str] = []
    for raw in output.split("\0"):
        relative = raw.strip()
        if not relative:
            continue
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError("tracked source contains an unsafe path")
        paths.append(candidate.as_posix())
    if not paths:
        raise RuntimeError("Workspace has no Git-tracked files")
    return tuple(sorted(set(paths)))


def _fingerprint(source: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        candidate = source / Path(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not candidate.exists():
            digest.update(b"<missing>")
            continue
        if candidate.is_symlink():
            raise RuntimeError("tracked symlinks are not accepted by the review snapshot")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_state(source: Path, *, workspace_id: int) -> str:
    status = _git(
        source,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        workspace_id=workspace_id,
    )
    return hashlib.sha256(status.encode("utf-8", errors="replace")).hexdigest()


def _copy_snapshot(source: Path, snapshot: Path, paths: tuple[str, ...]) -> None:
    for relative in paths:
        source_file = source / Path(relative)
        if not source_file.is_file() or source_file.is_symlink():
            continue
        target = snapshot / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)


def _make_snapshot_read_only(snapshot: Path) -> None:
    for path in sorted(snapshot.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        with contextlib.suppress(OSError):
            if path.is_file():
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            elif path.is_dir():
                path.chmod(
                    stat.S_IRUSR
                    | stat.S_IXUSR
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH
                )


def _bounded(value: str, limit: int = MAX_REVIEW_OUTPUT_BYTES) -> str:
    encoded = redact_text(value or "").encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8", errors="replace").strip()
    return encoded[:limit].decode("utf-8", errors="replace").rstrip() + "\n[output truncated]"


def _review_prompt(request: dict[str, Any], revision: str, fingerprint: str) -> str:
    context = request.get("context") or ""
    return (
        "You are an ephemeral, read-only peer reviewer. Inspect the disposable "
        "snapshot in your current directory. Do not edit, create, delete, rename, "
        "or format files. Do not access network services or credentials. Report "
        "findings first, ordered by severity, with repository-relative file:line "
        "evidence. If there are no findings, say so and name residual risks.\n\n"
        f"Request: {request['subject']}\n"
        f"Question: {request['question']}\n"
        f"Context: {context}\n"
        f"Snapshot revision: {revision}\n"
        f"Snapshot fingerprint: {fingerprint}\n"
    )


_TOOL_STATE_FILES: dict[str, tuple[str, ...]] = {
    "copilot": ("data.db", "config.json", "permissions-config.json"),
    "codex": (".credentials.json",),
    "claude": (".credentials.json",),
}


def _copy_tool_state(tool: str, destination: Path) -> Path:
    variable = {
        "copilot": "COPILOT_HOME",
        "codex": "CODEX_HOME",
        "claude": "CLAUDE_CONFIG_DIR",
    }[tool]
    default_name = {"copilot": ".copilot", "codex": ".codex", "claude": ".claude"}[tool]
    source = Path(os.environ.get(variable, "") or (Path.home() / default_name))
    target = destination / default_name.lstrip(".")
    target.mkdir(parents=True, exist_ok=True)
    for relative in _TOOL_STATE_FILES[tool]:
        source_file = source / relative
        if source_file.is_file():
            shutil.copy2(source_file, target / relative)
    return target


def _safe_env(tool: str, home: Path) -> dict[str, str]:
    keep = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "TERM",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "NODE_EXTRA_CA_CERTS",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    env["TEMP"] = str(home / "tmp")
    env["TMP"] = env["TEMP"]
    Path(env["TEMP"]).mkdir(parents=True, exist_ok=True)
    tool_home = _copy_tool_state(tool, home)
    if tool == "copilot":
        env["COPILOT_HOME"] = str(tool_home)
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    elif tool == "codex":
        env["CODEX_HOME"] = str(tool_home)
    elif tool == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(tool_home)
        if os.environ.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    return env


def _review_command(
    tool: str, prompt: str, snapshot: Path, scratch: Path
) -> tuple[list[str], str | None]:
    if tool == "copilot":
        return (
            [
                "copilot",
                "-C",
                str(snapshot),
                "-p",
                prompt,
                "--mode",
                "plan",
                "--no-ask-user",
                "--no-auto-update",
                "--no-custom-instructions",
                "--disable-builtin-mcps",
                "--available-tools=view,grep,glob",
                "--allow-all-tools",
                "--deny-tool=write",
                "--deny-tool=shell",
                "--deny-url=http://*",
                "--deny-url=https://*",
                "--silent",
                "--no-color",
                "--log-dir",
                str(scratch / "logs"),
            ],
            None,
        )
    if tool == "claude":
        return (
            [
                "claude",
                "-p",
                "--restricted",
                "--permission-mode",
                "plan",
                "--tools",
                "Read,Glob,Grep",
                "--no-session-persistence",
                "--safe-mode",
                "--strict-mcp-config",
                "--mcp-config",
                "{}",
                "--output-format",
                "text",
                prompt,
            ],
            None,
        )
    if tool == "codex":
        return (
            [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "-C",
                str(snapshot),
                "-",
            ],
            prompt,
        )
    raise ValueError(f"unsupported ephemeral review tool: {tool}")


def _run_review_process(
    argv: list[str],
    *,
    tool: str,
    request_code: str,
    session_id: str,
    snapshot: Path,
    home: Path,
    input_text: str | None,
    runtime_id: int | None = None,
):
    """Govern one reviewer launch whose only filesystem target is ``snapshot``."""
    from brains.exec import session_channel
    from brains.exec.guard import run

    def _observe(process: subprocess.Popen) -> None:
        session_channel.register(
            session_id, process, tool=tool, stdin_open=False, runtime_id=runtime_id
        )

    try:
        return run(
            argv,
            actor=f"help-review:{session_id}",
            action="help.review.execute",
            session_id=session_id,
            cwd=str(snapshot),
            env=_safe_env(tool, home),
            input_text=input_text,
            timeout=float(_review_timeout_seconds()),
            capture_output=True,
            idempotency_key=f"help-review:{request_code}:{session_id}",
            wait_for_approval=False,
            process_observer=_observe,
        )
    finally:
        session_channel.unregister(session_id)


def run_read_only_review(
    request: dict[str, Any],
    *,
    source_path: str,
    workspace_id: int,
    session_id: str,
    runtime_id: int | None = None,
) -> ReviewRun:
    """Run one provider against a disposable tracked-source snapshot."""
    source = Path(normalize_path(source_path))
    paths = _tracked_paths(source, workspace_id=workspace_id)
    before = _fingerprint(source, paths)
    source_state_before = _source_state(source, workspace_id=workspace_id)
    revision = _git(source, ["rev-parse", "HEAD"], workspace_id=workspace_id).strip()
    patch = _git(
        source,
        ["diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        workspace_id=workspace_id,
    )
    tool = str(request["required_tool"])
    try:
        with tempfile.TemporaryDirectory(
            prefix="brains-help-review-", ignore_cleanup_errors=True
        ) as temporary:
            scratch = Path(temporary)
            snapshot = scratch / "snapshot"
            home = scratch / "home"
            snapshot.mkdir()
            home.mkdir()
            _copy_snapshot(source, snapshot, paths)
            review_dir = snapshot / ".brains-review"
            review_dir.mkdir()
            (review_dir / "REQUEST.txt").write_text(
                _review_prompt(request, revision, before), encoding="utf-8"
            )
            (review_dir / "PATCH.diff").write_bytes(
                patch.encode("utf-8", errors="replace")[:MAX_PATCH_BYTES]
            )
            _make_snapshot_read_only(snapshot)
            prompt = _review_prompt(request, revision, before)
            argv, input_text = _review_command(tool, prompt, snapshot, scratch)
            outcome = _run_review_process(
                argv,
                tool=tool,
                request_code=str(request["code"]),
                session_id=session_id,
                snapshot=snapshot,
                home=home,
                input_text=input_text,
                runtime_id=runtime_id,
            )
            output = _bounded(outcome.stdout or outcome.stderr or outcome.reason)
            returncode = outcome.returncode if outcome.returncode is not None else 13
            error_code = None if outcome.allowed and returncode == 0 else "review_process_failed"
    except subprocess.TimeoutExpired:
        output = "Ephemeral reviewer exceeded its bounded runtime."
        returncode = 124
        error_code = "review_timeout"
    except Exception as exc:  # noqa: BLE001 - converted to bounded durable status
        output = f"Ephemeral reviewer could not run ({type(exc).__name__})."
        returncode = 1
        error_code = "review_setup_failed"
    unchanged = (
        _fingerprint(source, paths) == before
        and _source_state(source, workspace_id=workspace_id) == source_state_before
    )
    if not unchanged:
        return ReviewRun(
            answer="Review discarded because the registered source changed during execution.",
            evidence=f"request {request['code']}; source snapshot {before}; reviewer Session {session_id}",
            returncode=1,
            source_unchanged=False,
            error_code="source_changed_during_review",
        )
    if not output:
        output = "Reviewer returned no answer."
        error_code = error_code or "empty_review_output"
        returncode = returncode or 1
    return ReviewRun(
        answer=output,
        evidence=(
            f"request {request['code']}; tracked snapshot {before}; revision {revision}; "
            f"tool {tool}; reviewer Session {session_id}"
        ),
        returncode=returncode,
        source_unchanged=True,
        error_code=error_code,
    )


def _runtime_can_read_workspace(session, runtime: Runtime, workspace: Workspace) -> bool:
    if (
        runtime.status != "online"
        or runtime.health != "healthy"
        or runtime.org_id != workspace.org_id
        or not runtime.working_root
    ):
        return False
    try:
        root = normalize_path(runtime.working_root)
    except (OSError, ValueError):
        return False
    if root == workspace.path:
        return True
    return (
        session.query(WorkspaceAlias)
        .filter(WorkspaceAlias.workspace_id == workspace.id, WorkspaceAlias.path == root)
        .one_or_none()
        is not None
    )


def list_reviews_for_runtime(runtime_ref: str | int, limit: int = 10) -> list[dict[str, Any]]:
    from brains.control.assignments import _get_runtime

    init_db()
    now = utc_now()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        if (
            runtime.status != "online"
            or runtime.health != "healthy"
            or runtime.org_id is None
            or not runtime.working_root
        ):
            return []
        try:
            root = normalize_path(runtime.working_root)
        except (OSError, ValueError):
            return []
        matching_alias = select(WorkspaceAlias.id).where(
            WorkspaceAlias.workspace_id == Workspace.id,
            WorkspaceAlias.path == root,
        )
        rows = (
            session.query(HelpRequestExecution, HelpRequest, Workspace)
            .join(HelpRequest, HelpRequest.code == HelpRequestExecution.request_code)
            .join(Workspace, Workspace.id == HelpRequestExecution.source_workspace_id)
            .filter(
                HelpRequestExecution.status == "queued",
                HelpRequestExecution.required_tool == runtime.tool,
                HelpRequestExecution.launch_after <= now,
                HelpRequest.status == "open",
                HelpRequest.expires_at >= now,
                Workspace.org_id == runtime.org_id,
                or_(Workspace.path == root, matching_alias.exists()),
            )
            .order_by(HelpRequestExecution.launch_after, HelpRequest.created_at)
            .limit(max(1, min(limit, 25)))
            .all()
        )
        return [
            {
                "code": request.code,
                "subject": request.subject,
                "workspace_id": workspace.id,
                "workspace_path": runtime.working_root,
                "required_tool": execution.required_tool,
                "mode": execution.mode,
            }
            for execution, request, workspace in rows
        ]


def _claim(
    code: str,
    *,
    runtime: Runtime | None = None,
) -> dict[str, Any] | None:
    from brains.control.sessions import current_machine_id

    init_db()
    now = utc_now()
    with SessionLocal() as session:
        from brains.control.help import _expire_due

        _expire_due(session)
        session.commit()
        request = session.query(HelpRequest).filter(HelpRequest.code == code).one_or_none()
        execution = session.get(HelpRequestExecution, code)
        if request is None or execution is None or request.status != "open":
            return None
        if execution.status != "queued" or _as_utc(execution.launch_after) > now:
            return None
        workspace = session.get(Workspace, execution.source_workspace_id)
        if workspace is None:
            return None
        if runtime is not None:
            if runtime.tool != execution.required_tool:
                return None
            if not _runtime_can_read_workspace(session, runtime, workspace):
                return None
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        claimed_request = session.execute(
            update(HelpRequest)
            .where(
                HelpRequest.code == code,
                HelpRequest.status == "open",
                HelpRequest.expires_at >= now,
            )
            .values(
                status="claimed",
                claimed_by_session_id=session_id,
                claimed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(claimed_request, CursorResult) or claimed_request.rowcount != 1:
            session.rollback()
            return None
        claim_filters = [
            HelpRequestExecution.request_code == code,
            HelpRequestExecution.status == "queued",
            HelpRequestExecution.launch_after <= now,
        ]
        if runtime is not None:
            claim_filters.append(HelpRequestExecution.required_tool == runtime.tool)
        claimed_execution = session.execute(
            update(HelpRequestExecution)
            .where(*claim_filters)
            .values(
                status="running",
                runtime_id=runtime.id if runtime is not None else None,
                review_session_id=session_id,
                attempt=HelpRequestExecution.attempt + 1,
                lease_expires_at=now + timedelta(seconds=_review_lease_seconds()),
                started_at=now,
                updated_at=now,
                error_code=None,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(claimed_execution, CursorResult) or claimed_execution.rowcount != 1:
            session.rollback()
            return None
        requester = (
            session.get(AgentSession, request.from_session_id) if request.from_session_id else None
        )
        session.add(
            AgentSession(
                id=session_id,
                workspace_id=workspace.id,
                tool=execution.required_tool,
                pid=None,
                machine_id=runtime.machine_id if runtime is not None else current_machine_id(),
                runtime_id=runtime.id if runtime is not None else None,
                created_by_operator_id=(
                    requester.created_by_operator_id if requester is not None else None
                ),
                state="running",
                last_activity_at=now,
                metadata_json=json.dumps(
                    {
                        "kind": "ephemeral_help_review",
                        "request_code": code,
                        "read_only": True,
                    }
                ),
            )
        )
        session.commit()
        result = {
            "code": request.code,
            "subject": request.subject,
            "question": request.question,
            "context": request.context,
            "required_tool": execution.required_tool,
            "workspace_id": workspace.id,
            "workspace_path": runtime.working_root if runtime is not None else workspace.path,
            "session_id": session_id,
            "runtime_id": runtime.id if runtime is not None else None,
        }
    raw_workspace_id = result.get("workspace_id")
    if not isinstance(raw_workspace_id, int):
        raise ValueError(f"ephemeral help review has no Workspace: {code}")
    workspace_id = raw_workspace_id
    review_session_id = str(result["session_id"])
    append_event(
        "help_review_started",
        f"{code}: ephemeral {result['required_tool']} reviewer started",
        workspace_id=workspace_id,
        session_id=review_session_id,
        metadata={"code": code, "runtime_id": result["runtime_id"], "read_only": True},
    )
    return result


def _lock_review(session, code: str) -> tuple[HelpRequest | None, HelpRequestExecution | None]:
    """Serialize completion and lease recovery on the same two rows.

    Both transitions read ownership, status and the lease before writing, so
    they must not interleave: without this a worker completing just after its
    lease expired and :func:`dispatch_due_help_reviews` requeueing the same
    review can each observe a state the other is about to replace. A no-op
    write takes SQLite's writer lock before the read; PostgreSQL locks the rows.
    """
    request_query = session.query(HelpRequest).filter(HelpRequest.code == code)
    execution_query = session.query(HelpRequestExecution).filter(
        HelpRequestExecution.request_code == code
    )
    if session.get_bind().dialect.name == "postgresql":
        return request_query.with_for_update().one_or_none(), (
            execution_query.with_for_update().one_or_none()
        )
    session.execute(
        update(HelpRequestExecution)
        .where(HelpRequestExecution.request_code == code)
        .values(status=HelpRequestExecution.status)
    )
    return request_query.one_or_none(), execution_query.one_or_none()


def claim_review_for_runtime(runtime_ref: str | int, code: str) -> dict[str, Any] | None:
    from brains.control.assignments import _get_runtime

    init_db()
    with SessionLocal() as session:
        runtime = _get_runtime(session, runtime_ref)
        if runtime is None:
            raise ValueError(f"unknown runtime: {runtime_ref!r}")
        session.expunge(runtime)
    return _claim(code, runtime=runtime)


def complete_review(
    code: str,
    *,
    session_id: str,
    answer: str,
    evidence: str,
    returncode: int,
    source_unchanged: bool,
    error_code: str | None = None,
    runtime_id: int | None = None,
) -> dict[str, Any]:
    init_db()
    now = utc_now()
    clean_answer = _bounded(answer)
    clean_evidence = _bounded(evidence, 2000)
    succeeded = not error_code and returncode == 0 and source_unchanged and bool(clean_answer)
    with SessionLocal() as session:
        request, execution = _lock_review(session, code)
        review_session = session.get(AgentSession, session_id)
        if request is None or execution is None or review_session is None:
            raise ValueError(f"unknown ephemeral help review: {code}")
        if execution.review_session_id != session_id or request.claimed_by_session_id != session_id:
            raise ValueError(f"ephemeral help review is owned by another Session: {code}")
        if runtime_id is not None and execution.runtime_id != runtime_id:
            raise ValueError(f"ephemeral help review is owned by another Runtime: {code}")
        if request.status == "answered" and execution.status == "answered":
            return {"code": code, "status": "answered", "duplicate": True}
        if request.status != "claimed" or execution.status != "running":
            raise ValueError(f"ephemeral help review is not running: {code}")
        if execution.lease_expires_at is None or _as_utc(execution.lease_expires_at) <= now:
            # Recovery owns an expired lease: answering here would race a
            # requeue that may already have handed the review to somebody else.
            raise ValueError(f"ephemeral help review lease has expired: {code}")
        review_session.ended_at = now
        review_session.state = "completed" if succeeded else "failed"
        review_session.summary = (
            "ephemeral read-only review answered"
            if succeeded
            else f"ephemeral read-only review failed ({error_code or returncode})"
        )
        execution.completed_at = now
        execution.lease_expires_at = None
        execution.updated_at = now
        execution.error_code = error_code
        if succeeded:
            request.answer = clean_answer
            request.evidence = clean_evidence
            request.answered_at = now
            request.status = "answered"
            execution.status = "answered"
        else:
            request.status = "open"
            request.claimed_by_session_id = None
            request.claimed_at = None
            execution.runtime_id = None
            execution.review_session_id = None
            if execution.attempt < MAX_REVIEW_ATTEMPTS:
                execution.status = "queued"
                execution.launch_after = now + timedelta(seconds=1)
                request.expires_at = now + timedelta(seconds=_review_lease_seconds())
            else:
                execution.status = "failed"
        session.commit()
        result = {
            "code": code,
            "status": request.status,
            "execution_status": execution.status,
            "review_session_id": session_id,
            "duplicate": False,
        }
        workspace_id = execution.source_workspace_id
    append_event(
        "help_answered" if succeeded else "help_review_failed",
        (
            f"{code}: answered by ephemeral reviewer {session_id}"
            if succeeded
            else f"{code}: ephemeral reviewer failed"
        ),
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": code, "error_code": error_code, "source_unchanged": source_unchanged},
    )
    return result


def run_local_review(code: str) -> dict[str, Any] | None:
    raise ValueError("ephemeral peer execution is withdrawn")


def _withdrawn_run_local_review_compat(code: str) -> dict[str, Any] | None:
    """Unreachable historical implementation retained for data-boundary archaeology."""
    with SessionLocal() as session:
        execution = session.get(HelpRequestExecution, code)
        if execution is None or shutil.which(execution.required_tool) is None:
            return None
    claim = _claim(code)
    if claim is None:
        return None
    try:
        result = run_read_only_review(
            claim,
            source_path=str(claim["workspace_path"]),
            workspace_id=int(claim["workspace_id"]),
            session_id=str(claim["session_id"]),
        )
    except Exception as exc:  # noqa: BLE001 - settle the durable attempt
        result = ReviewRun(
            answer=f"Ephemeral reviewer could not run ({type(exc).__name__}).",
            evidence=f"request {code}; reviewer Session {claim['session_id']}",
            returncode=1,
            source_unchanged=True,
            error_code="review_setup_failed",
        )
    return complete_review(
        code,
        session_id=str(claim["session_id"]),
        runtime_id=None,
        answer=result.answer,
        evidence=result.evidence,
        returncode=result.returncode,
        source_unchanged=result.source_unchanged,
        error_code=result.error_code,
    )


def _worker(code: str) -> None:
    try:
        with SessionLocal() as session:
            execution = session.get(HelpRequestExecution, code)
            if execution is None:
                return
            delay = max(0.0, (_as_utc(execution.launch_after) - utc_now()).total_seconds())
        if delay:
            time_event = threading.Event()
            time_event.wait(delay)
        run_local_review(code)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(code)


def schedule_help_review(code: str) -> bool:
    """Refuse historical automatic-review launch requests."""
    return False


def _withdrawn_schedule_help_review_compat(code: str) -> bool:
    """Unreachable historical implementation retained for data-boundary archaeology."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    with _INFLIGHT_LOCK:
        if code in _INFLIGHT:
            return False
        _INFLIGHT.add(code)
    try:
        threading.Thread(
            target=_worker,
            args=(code,),
            name=f"brains-help-review-{code}",
            daemon=True,
        ).start()
    except Exception:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(code)
        return False
    return True


def dispatch_due_help_reviews(limit: int = 5) -> list[str]:
    """Ignore historical queued review rows without launching processes."""
    return []


def _withdrawn_dispatch_due_help_reviews_compat(limit: int = 5) -> list[str]:
    """Unreachable historical implementation retained for data-boundary archaeology."""
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        stale = (
            session.query(HelpRequestExecution.request_code)
            .join(HelpRequest, HelpRequest.code == HelpRequestExecution.request_code)
            .filter(
                HelpRequestExecution.status == "running",
                HelpRequestExecution.lease_expires_at < now,
                HelpRequest.status == "claimed",
            )
            .all()
        )
        for (stale_code,) in stale:
            request, execution = _lock_review(session, stale_code)
            if request is None or execution is None:
                continue
            # Re-read under the lock: a worker may have answered between the
            # scan above and this transition.
            if (
                execution.status != "running"
                or request.status != "claimed"
                or execution.lease_expires_at is None
                or _as_utc(execution.lease_expires_at) >= now
            ):
                continue
            review_session = (
                session.get(AgentSession, execution.review_session_id)
                if execution.review_session_id
                else None
            )
            if review_session is not None and review_session.ended_at is None:
                review_session.ended_at = now
                review_session.state = "failed"
                review_session.summary = "ephemeral help review lease expired"
            request.status = "open"
            request.claimed_by_session_id = None
            request.claimed_at = None
            execution.runtime_id = None
            execution.review_session_id = None
            execution.lease_expires_at = None
            execution.error_code = "review_lease_expired"
            execution.updated_at = now
            if execution.attempt >= MAX_REVIEW_ATTEMPTS:
                execution.status = "failed"
                execution.completed_at = now
            else:
                execution.status = "queued"
                execution.launch_after = now
                request.expires_at = now + timedelta(seconds=_review_lease_seconds())
        session.commit()
        rows = (
            session.query(HelpRequestExecution)
            .join(HelpRequest, HelpRequest.code == HelpRequestExecution.request_code)
            .filter(
                HelpRequestExecution.status == "queued",
                HelpRequestExecution.launch_after <= now,
                HelpRequest.status == "open",
            )
            .order_by(HelpRequestExecution.launch_after)
            .limit(max(1, min(limit, 25)))
            .all()
        )
        codes = [row.request_code for row in rows if shutil.which(row.required_tool)]
    return [code for code in codes if schedule_help_review(code)]


__all__ = [
    "ReviewRun",
    "claim_review_for_runtime",
    "complete_review",
    "dispatch_due_help_reviews",
    "list_reviews_for_runtime",
    "run_local_review",
    "run_read_only_review",
    "schedule_help_review",
]
