from __future__ import annotations

import contextlib
import json
import os
import platform
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func

from brains.control.common import normalize_path, slug_from_path, unique_slug, utc_now
from brains.control.events import append_event
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    AgentTask,
    Handoff,
    Org,
    SessionLease,
    SessionSuccessor,
    TopicSubscription,
    Workspace,
    WorkspaceAlias,
    WorkspaceClaim,
)

if TYPE_CHECKING:
    from brains.control.operators import OperatorRecord


class WorkspaceNotFoundError(ValueError):
    pass


class AgentSessionNotFoundError(ValueError):
    pass


# Filesystem markers that identify a path as a legitimate project root.
# Used by :func:`has_project_marker` so :func:`register_workspace` can emit a
# warning event when an agent auto-registers an "umbrella" folder (e.g. the
# parent dir of several repos) that isn't really a project on its own.
WORKSPACE_PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "mix.exs",
    "composer.json",
    "requirements.txt",
    "setup.py",
    ".hg",
    ".svn",
)

STALE_SESSION_TTL_SECONDS = 30 * 60
DORMANT_SESSION_STATE = "dormant"


def _fallback_machine_id() -> str:
    value = platform.node().strip() or "unknown-machine"
    return value[:64]


def current_machine_id() -> str:
    """Return a stable per-machine identifier persisted under the brains state dir."""
    from brains.api.admin_key import state_dir

    path = state_dir() / "machine-id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing[:64]
        machine_id = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(machine_id + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return machine_id
    except (OSError, UnicodeError):
        return _fallback_machine_id()


def has_project_marker(path: str) -> bool:
    """Return True if ``path`` looks like a real project root.

    Checks for any of the well-known markers in
    :data:`WORKSPACE_PROJECT_MARKERS` either as a sibling file/folder or
    via a suffix match (``*.csproj``, ``*.sln`` style). Returns False when
    ``path`` doesn't exist on disk.
    """
    if not path or not os.path.isdir(path):
        return False
    try:
        entries = set(os.listdir(path))
    except OSError:
        return False
    for marker in WORKSPACE_PROJECT_MARKERS:
        if marker in entries:
            return True
    for entry in entries:
        lowered = entry.lower()
        if lowered.endswith((".csproj", ".sln", ".fsproj", ".vbproj")):
            return True
    return False


def workspace_identity(path: str) -> str:
    """Return a stable local identity shared by linked Git worktrees."""
    normalized = normalize_path(path)
    try:
        git_entry = Path(normalized, ".git")
        if git_entry.is_dir():
            common_dir = git_entry
        elif git_entry.is_file():
            marker = git_entry.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                raise ValueError("invalid .git pointer")
            git_dir = Path(marker.removeprefix("gitdir:").strip())
            if not git_dir.is_absolute():
                git_dir = git_entry.parent / git_dir
            git_dir = git_dir.resolve()
            common_marker = git_dir / "commondir"
            if common_marker.is_file():
                common_dir = (git_dir / common_marker.read_text(encoding="utf-8").strip()).resolve()
            else:
                common_dir = git_dir
        else:
            common_dir = None
    except (OSError, UnicodeError, ValueError):
        common_dir = None
    if common_dir is not None:
        return f"git:{os.path.normcase(str(common_dir.resolve()))}"
    return f"path:{os.path.normcase(normalized)}"


def _git_worktree_paths(path: str) -> tuple[str, ...]:
    """Return linked worktree roots from Git's local metadata."""
    identity = workspace_identity(path)
    if not identity.startswith("git:"):
        return ()
    common_dir = Path(identity.removeprefix("git:"))
    roots: set[str] = set()
    try:
        if common_dir.name == ".git":
            roots.add(normalize_path(str(common_dir.parent)))
        worktrees_dir = common_dir / "worktrees"
        if worktrees_dir.is_dir():
            for entry in worktrees_dir.iterdir():
                gitdir_file = entry / "gitdir"
                if not gitdir_file.is_file():
                    continue
                git_entry = Path(gitdir_file.read_text(encoding="utf-8").strip())
                roots.add(normalize_path(str(git_entry.parent)))
    except (OSError, UnicodeError, ValueError):
        return ()
    return tuple(sorted(roots))


def _record_workspace_alias(session, workspace: Workspace, path: str, identity_key: str) -> None:
    alias = session.query(WorkspaceAlias).filter(WorkspaceAlias.path == path).one_or_none()
    if alias is None:
        session.add(WorkspaceAlias(workspace_id=workspace.id, path=path, identity_key=identity_key))
    else:
        alias.workspace_id = workspace.id
        alias.identity_key = identity_key


def _resolve_workspace_path(session, path: str) -> Workspace | None:
    normalized = normalize_path(path)
    alias = session.query(WorkspaceAlias).filter(WorkspaceAlias.path == normalized).one_or_none()
    if alias is not None:
        return session.get(Workspace, alias.workspace_id)
    return session.query(Workspace).filter(Workspace.path == normalized).one_or_none()


def _pid_alive(pid: int) -> bool:
    """Cross-platform check that ``pid`` belongs to a live process.

    Ported from agent-hivemind ``agent_hivemind.db._pid_alive``. Returns
    ``False`` for non-positive PIDs so the reaper treats sessions with a
    missing or sentinel ``pid`` as candidates for reaping only when the
    caller already checked the column is populated.

    On Windows we use ``OpenProcess`` + ``GetExitCodeProcess`` because
    ``os.kill(pid, 0)`` is unreliable there. On POSIX we use the standard
    ``os.kill(pid, 0)`` signal-0 trick: ``PermissionError`` still means
    the PID exists (we just can't signal it).
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            # ``ctypes.windll`` only exists on Windows; reaching it through
            # ``getattr`` keeps the module type-checkable on every platform.
            windll = getattr(ctypes, "windll", None)
            if windll is None:  # pragma: no cover - Windows-only attribute
                raise AttributeError("ctypes.windll")
            kernel32 = windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Fall back to POSIX-style probe if ctypes can't load kernel32
            # (extremely unusual; tests under emulation can hit this).
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but we lack permission to signal it.
        return True


def reap_zombie_sessions() -> list[str]:
    """Mark crashed sessions as ended, release their claims and tasks.

    A session is "zombie" only when **two** signals agree it is gone:

    * its recorded PID is no longer alive on this machine (or, for a
      foreign-machine session, its heartbeat is stale), AND
    * its opportunistic heartbeat — ``last_activity_at``, falling back to
      ``started_at`` — is older than ``STALE_SESSION_TTL_SECONDS``.

    The heartbeat gate exists because the recorded PID is frequently the
    brains stdio child rather than the agent itself, so a dead PID alone
    proves nothing: field reports showed actively-messaging sessions reaped
        nine seconds after their last message. Fresh activity always wins;
        a quiet session with a dead PID is the only thing this reaper takes.

    For each such session we:

    * Stamp ``ended_at = now`` and append a ``zombie reaped`` note to the
      session summary.
    * Delete every ``workspace_claims`` row owned by the dead session.
    * Flip every ``agent_tasks`` row claimed by the dead session in
      ``in_progress`` state back to ``available`` so another agent can pick
      it up.
    * Emit a ``session_reaped`` event for the audit ledger.

    Returns the list of reaped session IDs. Safe to call on a fresh database
    (returns an empty list).
    """
    init_db()
    reaped: list[str] = []
    reaped_workspaces: dict[str, int] = {}
    machine_id = current_machine_id()
    with SessionLocal() as session:
        live_rows = session.query(AgentSession).filter(AgentSession.ended_at.is_(None)).all()
        now = utc_now()
        stale_cutoff = now - timedelta(seconds=STALE_SESSION_TTL_SECONDS)
        for row in live_rows:
            pid = row.pid
            note: str
            if (
                pid is None
                and row.runtime_id is None
                and row.issue_id is None
                and row.persona_id is None
            ):
                # Coordination handles use renewable leases and become dormant;
                # only process-bound execution Sessions are terminally reaped.
                continue
            if row.machine_id and row.machine_id != machine_id:
                last_activity = row.last_activity_at
                if last_activity is None:
                    # No heartbeat evidence yet; don't guess from a foreign PID.
                    continue
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=now.tzinfo)
                if last_activity >= stale_cutoff:
                    continue
                note = (
                    "zombie reaped: foreign machine "
                    f"{row.machine_id} stale for >{STALE_SESSION_TTL_SECONDS}s"
                )
            else:
                if pid is None or pid <= 0:
                    # No PID recorded — can't prove the local/legacy session is dead.
                    continue
                if _pid_alive(pid):
                    continue
                # PID dead is necessary but not sufficient: the recorded pid
                # may be a short-lived launcher/stdio child while the agent
                # keeps working. Require the heartbeat to be stale too.
                last_activity = row.last_activity_at or row.started_at
                if last_activity is None:
                    continue
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=now.tzinfo)
                if last_activity >= stale_cutoff:
                    continue
                note = (
                    f"zombie reaped: pid {pid} dead and no activity for "
                    f">{STALE_SESSION_TTL_SECONDS}s"
                )
            row.ended_at = now
            if getattr(row, "state", None) not in _TERMINAL_SESSION_STATES:
                # Reaping is a terminal path: ``ended_at`` and the explicit
                # state must move together or the store grows contradictory
                # Sessions that read as running forever (BL-P0-07).
                row.state = "failed"
            row.summary = f"{row.summary}\n{note}".strip() if row.summary else note
            session.query(WorkspaceClaim).filter(WorkspaceClaim.session_id == row.id).delete(
                synchronize_session=False
            )
            session.query(AgentTask).filter(
                AgentTask.claimed_by_session_id == row.id,
                AgentTask.status == "in_progress",
            ).update(
                {
                    "status": "available",
                    "claimed_by_session_id": None,
                    "claimed_at": None,
                },
                synchronize_session=False,
            )
            reaped.append(row.id)
            reaped_workspaces[row.id] = row.workspace_id
        if reaped:
            session.commit()
    for sid in reaped:
        append_event(
            "session_reaped",
            f"zombie session reaped: {sid}",
            workspace_id=reaped_workspaces.get(sid),
            session_id=sid,
        )
    return reaped


def _release_session_ownership(session, session_id: str) -> None:
    session.query(WorkspaceClaim).filter(WorkspaceClaim.session_id == session_id).delete(
        synchronize_session=False
    )
    session.query(AgentTask).filter(
        AgentTask.claimed_by_session_id == session_id,
        AgentTask.status == "in_progress",
    ).update(
        {"status": "available", "claimed_by_session_id": None, "claimed_at": None},
        synchronize_session=False,
    )


def _supersede_coordination_handle(
    session,
    predecessor: AgentSession,
    successor: AgentSession,
) -> None:
    """Move one active coordination handle's ownership to its successor."""
    coordination_handle = (
        predecessor.pid is None
        and predecessor.runtime_id is None
        and predecessor.issue_id is None
        and predecessor.persona_id is None
    )
    if not coordination_handle or session.get(SessionLease, predecessor.id) is None:
        return
    for subscription in (
        session.query(TopicSubscription)
        .filter(TopicSubscription.session_id == predecessor.id)
        .all()
    ):
        existing = session.get(TopicSubscription, (successor.id, subscription.topic))
        if existing is None:
            subscription.session_id = successor.id
        else:
            existing.last_seen_post_id = max(
                existing.last_seen_post_id,
                subscription.last_seen_post_id,
            )
            existing.subscribed_at = min(existing.subscribed_at, subscription.subscribed_at)
            existing.updated_at = max(existing.updated_at, subscription.updated_at)
            session.delete(subscription)
    if predecessor.ended_at is not None:
        return
    predecessor.state = DORMANT_SESSION_STATE
    session.query(WorkspaceClaim).filter(WorkspaceClaim.session_id == predecessor.id).update(
        {"session_id": successor.id}, synchronize_session=False
    )
    session.query(AgentTask).filter(
        AgentTask.claimed_by_session_id == predecessor.id,
        AgentTask.status == "in_progress",
    ).update({"claimed_by_session_id": successor.id}, synchronize_session=False)


def sweep_stale_session_leases() -> list[str]:
    """Make expired PID-less coordination Sessions dormant and release ownership."""
    init_db()
    now = utc_now()
    dormant: list[tuple[str, int]] = []
    with SessionLocal() as session:
        rows = (
            session.query(AgentSession.id, AgentSession.workspace_id)
            .join(SessionLease, SessionLease.session_id == AgentSession.id)
            .filter(
                AgentSession.ended_at.is_(None),
                AgentSession.pid.is_(None),
                AgentSession.runtime_id.is_(None),
                AgentSession.issue_id.is_(None),
                AgentSession.persona_id.is_(None),
                AgentSession.state != DORMANT_SESSION_STATE,
                SessionLease.lease_expires_at < now,
            )
            .all()
        )
        lease_is_still_expired = (
            session.query(SessionLease)
            .filter(
                SessionLease.session_id == AgentSession.id,
                SessionLease.lease_expires_at < now,
            )
            .exists()
        )
        for session_id, workspace_id in rows:
            updated = (
                session.query(AgentSession)
                .filter(
                    AgentSession.id == session_id,
                    AgentSession.ended_at.is_(None),
                    AgentSession.state != DORMANT_SESSION_STATE,
                    lease_is_still_expired,
                )
                .update(
                    {"state": DORMANT_SESSION_STATE},
                    synchronize_session=False,
                )
            )
            if not updated:
                continue
            _release_session_ownership(session, session_id)
            dormant.append((session_id, workspace_id))
        if dormant:
            session.commit()
    for session_id, workspace_id in dormant:
        append_event(
            "session_dormant",
            "coordination session lease expired",
            workspace_id=workspace_id,
            session_id=session_id,
            renew_session=False,
        )
        _cancel_open_commands(
            session_id,
            reason="the coordination Session lease expired before the command was delivered",
            result="session_dormant",
        )
    return [session_id for session_id, _workspace_id in dormant]


SESSION_LIVE_TTL_SECONDS = 60 * 60


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _post_end_activity(row: AgentSession) -> bool:
    activity = _aware(row.last_activity_at)
    ended = _aware(row.ended_at)
    return bool(activity and ended and activity > ended + timedelta(seconds=1))


def _recent_activity(row: AgentSession, now: datetime | None = None) -> bool:
    activity = _aware(row.last_activity_at or row.started_at)
    current = now or utc_now()
    return bool(activity and activity >= current - timedelta(seconds=SESSION_LIVE_TTL_SECONDS))


def _terminal_or_ended(row: AgentSession) -> bool:
    if row.ended_at is None:
        return bool(
            getattr(row, "state", None) in _TERMINAL_SESSION_STATES and not _recent_activity(row)
        )
    terminal = True
    # Field-report recovery: demonstrable post-end activity invalidates a stale
    # terminal flag. An ordinary end event at the same timestamp does not.
    return bool(terminal and not (_post_end_activity(row) and _recent_activity(row)))


def live_replacement_session_ids(
    session, workspace_id: int | None, exclude_session_id: str | None = None
) -> list[str]:
    """Live sessions in the same workspace, freshest first (bounded).

    Surfaced in dead-handle errors so a caller can recover without the
    discovery dance — the single most expensive step in field report #2.
    """
    cutoff = utc_now() - timedelta(seconds=SESSION_LIVE_TTL_SECONDS)
    q = session.query(AgentSession).filter(
        AgentSession.workspace_id == workspace_id,
        func.coalesce(AgentSession.last_activity_at, AgentSession.started_at) >= cutoff,
    )
    if exclude_session_id:
        q = q.filter(AgentSession.id != exclude_session_id)
    q = q.order_by(AgentSession.last_activity_at.desc().nullslast()).limit(5)
    from brains.control.session_liveness import lease_is_current

    out: list[str] = []
    for row in q.all():
        if _terminal_or_ended(row) or row.state == DORMANT_SESSION_STATE:
            continue
        if row.pid is None:
            lease = session.get(SessionLease, row.id)
            if lease is not None and not lease_is_current(lease):
                continue
        out.append(row.id)
    return out


def require_live_session(
    session,
    session_id: str | None,
    *,
    action: str,
    renew_lease: bool = True,
) -> AgentSession:
    """Raise a loud, actionable error unless ``session_id`` names a live session.

    Every agent-facing surface that takes a session handle as *attribution*
    or *recipient* validates through this: a dead handle must never be
    silently accepted (field report #2: brains let messages be sent AS a
    reaped session, and dead-handle reads were indistinguishable from an
    empty inbox). The error carries ended_at, the recorded reason, and the
    workspace's live replacement candidates.
    """
    if not session_id:
        raise ValueError(f"{action} requires a session id")
    row = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    if row is None:
        raise ValueError(f"unknown session: {session_id} ({action})")
    if not _terminal_or_ended(row):
        if row.pid is None:
            from brains.control.session_liveness import lease_is_current, renew_session_lease

            lease = session.get(SessionLease, row.id)
            if row.state == DORMANT_SESSION_STATE or (
                lease is not None and not lease_is_current(lease)
            ):
                successor = session.get(SessionSuccessor, row.id)
                replacements = live_replacement_session_ids(
                    session, row.workspace_id, exclude_session_id=row.id
                )
                successor_note = (
                    f"; explicit successor: {successor.successor_session_id}"
                    if successor is not None
                    else ""
                )
                raise ValueError(
                    f"session {session_id} is dormant or expired; refusing {action} against a "
                    "stale handle. "
                    f"live replacement candidates in the same workspace: "
                    f"{replacements if replacements else '[]'}{successor_note}"
                )
            if renew_lease:
                renew_session_lease(session, row)
        if row.ended_at is not None or row.state in _TERMINAL_SESSION_STATES:
            row.ended_at = None
            row.state = "running"
        return row
    replacements = live_replacement_session_ids(session, row.workspace_id)
    reason = (row.summary or "").strip()
    reason_tail = f"; reason: {reason[-160:]}" if reason else ""
    raise ValueError(
        f"session {session_id} is ended (state={getattr(row, 'state', None)}, "
        f"ended_at={row.ended_at.isoformat() if row.ended_at else None}{reason_tail}); "
        f"refusing {action} against a dead handle. "
        f"live replacement candidates in the same workspace: "
        f"{replacements if replacements else '[]'}"
    )


def register_workspace(
    path: str,
    slug: str | None = None,
    name: str | None = None,
    org_id: int | None = None,
) -> Workspace:
    init_db()
    normalized = normalize_path(path)
    identity_key = workspace_identity(normalized)
    archived_duplicate_ids: list[int] = []
    with SessionLocal() as session:
        aliases = (
            session.query(WorkspaceAlias, Workspace)
            .join(Workspace, Workspace.id == WorkspaceAlias.workspace_id)
            .filter(WorkspaceAlias.identity_key == identity_key)
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
            .all()
        )
        candidates = [workspace for _alias, workspace in aliases]
        if identity_key.startswith("git:"):
            worktree_paths = _git_worktree_paths(normalized)
            if worktree_paths:
                linked = (
                    session.query(Workspace)
                    .filter(Workspace.path.in_(worktree_paths))
                    .order_by(Workspace.created_at.asc(), Workspace.id.asc())
                    .all()
                )
                for candidate in linked:
                    if candidate not in candidates:
                        candidates.append(candidate)
        exact = _resolve_workspace_path(session, normalized)
        if exact is not None and exact not in candidates:
            candidates.append(exact)
        if candidates:
            org_ids = {candidate.org_id for candidate in candidates}
            if len(org_ids) > 1:
                raise ValueError("repository paths are registered to multiple organizations")
            existing = min(candidates, key=lambda row: (row.created_at, row.id))
            if org_id is not None and existing.org_id != org_id:
                raise ValueError("repository is already registered to another organization")
            if os.path.isdir(normalized):
                existing.status = "active"
            _record_workspace_alias(session, existing, existing.path, identity_key)
            _record_workspace_alias(session, existing, normalized, identity_key)
            for duplicate in candidates:
                if duplicate.id != existing.id:
                    session.query(WorkspaceAlias).filter(
                        WorkspaceAlias.workspace_id == duplicate.id
                    ).update(
                        {
                            WorkspaceAlias.workspace_id: existing.id,
                            WorkspaceAlias.identity_key: identity_key,
                        },
                        synchronize_session=False,
                    )
                    if duplicate.status != "archived":
                        duplicate.status = "archived"
                        archived_duplicate_ids.append(duplicate.id)
            session.commit()
            session.refresh(existing)
            if archived_duplicate_ids:
                append_event(
                    "workspace_alias_converged",
                    f"workspace aliases converged on {existing.slug}",
                    workspace_id=existing.id,
                    metadata={"archived_workspace_ids": archived_duplicate_ids},
                )
            return existing
        all_slugs = {row.slug for row in session.query(Workspace.slug).all()}
        final_slug = slug or unique_slug(slug_from_path(normalized), all_slugs)
        # Every Workspace creation path assigns valid Org scope (BL-P0-07):
        # an explicit Org must exist, otherwise the install's default Org is
        # used, matching the one-time backfill in migration 120.
        if org_id is None:
            from brains.control.orgs import ensure_default_org

            resolved_org_id = int(ensure_default_org()["id"])
        else:
            org = session.query(Org).filter(Org.id == org_id).one_or_none()
            if org is None:
                raise ValueError(f"unknown org: {org_id}")
            resolved_org_id = org.id
        workspace = Workspace(
            slug=final_slug,
            path=normalized,
            name=name or final_slug,
            status="active",
            org_id=resolved_org_id,
        )
        session.add(workspace)
        session.flush()
        _record_workspace_alias(session, workspace, normalized, identity_key)
        session.commit()
        session.refresh(workspace)
    has_marker = has_project_marker(normalized)
    append_event(
        "workspace_registered",
        f"workspace registered: {final_slug}",
        workspace_id=workspace.id,
        metadata={"path": normalized, "has_project_marker": has_marker},
    )
    # Soft warning when an agent auto-registers a path that doesn't look like
    # a real project root (e.g. umbrella parent folder). Doesn't refuse —
    # keeps existing flows working — but ``brains workspaces doctor`` surfaces
    # these so operators can prune.
    if not has_marker:
        append_event(
            "workspace_registered_no_marker",
            (
                f"workspace registered without project marker: {final_slug} "
                f"({normalized}) — looks like an umbrella folder or empty "
                "directory; inspect it with `brains-ai workspaces doctor`."
            ),
            workspace_id=workspace.id,
            metadata={"path": normalized},
        )
    return workspace


def get_workspace(
    path: str | None = None, slug: str | None = None, id: int | None = None
) -> Workspace:
    init_db()
    with SessionLocal() as session:
        query = session.query(Workspace)
        if id is not None:
            row = query.filter(Workspace.id == id).one_or_none()
        elif slug is not None:
            row = query.filter(Workspace.slug == slug).one_or_none()
        elif path is not None:
            row = _resolve_workspace_path(session, path)
        else:
            row = None
        if row is None:
            raise WorkspaceNotFoundError("workspace not found")
        return row


def list_workspaces(*, org_id: int | None = None, include_archived: bool = False) -> list[dict]:
    init_db()
    with SessionLocal() as session:
        query = session.query(Workspace)
        if org_id is not None:
            query = query.filter(Workspace.org_id == org_id)
        if not include_archived:
            query = query.filter(Workspace.status == "active")
        rows = query.order_by(Workspace.name, Workspace.slug).all()
        return [
            {
                "id": row.id,
                "slug": row.slug,
                "name": row.name,
                "path": row.path,
                "status": row.status,
                "visibility": row.visibility,
                "org_id": row.org_id,
                "last_touched_at": (
                    row.last_touched_at.isoformat() if row.last_touched_at else None
                ),
            }
            for row in rows
        ]


def start_session(
    workspace_path: str,
    tool: str = "codex",
    pid: int | None = None,
    metadata: dict[str, Any] | None = None,
    operator: str | None = None,
    predecessor_session_id: str | None = None,
    reuse_existing: bool = False,
    auto_link_predecessor: bool = False,
    lease_session: bool = True,
) -> dict:
    workspace = register_workspace(workspace_path)
    # Resolve who owns this session before we open it so the row can be
    # stamped with ``created_by_operator_id``. Defaults to the auto-
    # provisioned ``admin`` operator on any single-operator install —
    # see ``brains.control.operators.resolve_current_operator``.
    from brains.control.operators import resolve_current_operator

    operator_record = resolve_current_operator(operator=operator)
    # Sweep zombies before opening a new session so the new agent inherits
    # a clean view of workspace claims and task locks.
    reap_zombie_sessions()
    sweep_stale_session_leases()
    # Decay stale handoffs so the welcome packet never advertises an
    # ancient "active" handoff as something the new session should pick.
    try:
        from brains.control.handoffs import mark_stale_handoffs

        mark_stale_handoffs()
    except Exception:
        pass
    if reuse_existing and predecessor_session_id is None and pid is None:
        from brains.control.session_liveness import lease_is_current, renew_session_lease

        with SessionLocal() as db_session:
            has_successor = (
                db_session.query(SessionSuccessor)
                .filter(SessionSuccessor.predecessor_session_id == AgentSession.id)
                .exists()
            )
            has_lease = (
                db_session.query(SessionLease)
                .filter(SessionLease.session_id == AgentSession.id)
                .exists()
            )
            candidates = (
                db_session.query(AgentSession)
                .filter(
                    AgentSession.workspace_id == workspace.id,
                    AgentSession.tool == tool,
                    AgentSession.created_by_operator_id == operator_record["id"],
                    AgentSession.pid.is_(None),
                    AgentSession.runtime_id.is_(None),
                    AgentSession.issue_id.is_(None),
                    AgentSession.persona_id.is_(None),
                    AgentSession.ended_at.is_(None),
                    ~has_successor,
                    has_lease,
                )
                .order_by(
                    AgentSession.last_activity_at.desc().nullslast(),
                    AgentSession.started_at.desc(),
                )
                .all()
            )
            current = [
                row
                for row in candidates
                if row.state != DORMANT_SESSION_STATE
                and lease_is_current(db_session.get(SessionLease, row.id))
            ]
            if len(current) > 1:
                raise ValueError(
                    "multiple live coordination sessions match this workspace/tool/operator: "
                    f"{[row.id for row in current]}; resume one explicitly"
                )
            reusable = current[0] if current else (candidates[0] if candidates else None)
            if reusable is not None:
                renew_session_lease(db_session, reusable)
                db_session.commit()
                return _session_registration_result(
                    workspace,
                    reusable.id,
                    tool,
                    operator_record,
                    predecessor_session_id=None,
                    reused=True,
                )

    session_id = f"ses_{uuid.uuid4().hex[:12]}"
    init_db()
    with SessionLocal() as db_session:
        if auto_link_predecessor and predecessor_session_id is None:
            has_successor = (
                db_session.query(SessionSuccessor)
                .filter(SessionSuccessor.predecessor_session_id == AgentSession.id)
                .exists()
            )
            has_lease = (
                db_session.query(SessionLease)
                .filter(SessionLease.session_id == AgentSession.id)
                .exists()
            )
            predecessors = (
                db_session.query(AgentSession)
                .filter(
                    AgentSession.workspace_id == workspace.id,
                    AgentSession.tool == tool,
                    AgentSession.created_by_operator_id == operator_record["id"],
                    AgentSession.pid.is_(None),
                    AgentSession.runtime_id.is_(None),
                    AgentSession.issue_id.is_(None),
                    AgentSession.persona_id.is_(None),
                    AgentSession.id != session_id,
                    ~has_successor,
                    has_lease,
                )
                .order_by(
                    AgentSession.last_activity_at.desc().nullslast(),
                    AgentSession.started_at.desc(),
                )
                .all()
            )
            from brains.control.session_liveness import lease_is_current

            current_predecessors = [
                candidate
                for candidate in predecessors
                if candidate.ended_at is None
                and candidate.state != DORMANT_SESSION_STATE
                and lease_is_current(db_session.get(SessionLease, candidate.id))
            ]
            if len(current_predecessors) > 1:
                raise ValueError(
                    "multiple live coordination sessions match this workspace/tool/operator: "
                    f"{[candidate.id for candidate in current_predecessors]}; "
                    "resume or end one explicitly"
                )
            predecessor = (
                current_predecessors[0]
                if current_predecessors
                else (predecessors[0] if predecessors else None)
            )
            predecessor_session_id = predecessor.id if predecessor is not None else None
        row = AgentSession(
            id=session_id,
            workspace_id=workspace.id,
            tool=tool,
            # Only a caller that owns a durable process may bind its PID.
            # Bare CLI/MCP registration runs in a short-lived helper process;
            # recording that helper's PID makes an active agent look dead as
            # soon as the command exits and lets the reaper destroy its claim,
            # task ownership, and mailbox after one quiet interval.
            pid=pid,
            machine_id=current_machine_id(),
            created_by_operator_id=operator_record["id"],
            metadata_json=json.dumps(metadata or {}),
        )
        db_session.add(row)
        db_session.flush()
        from brains.control.session_liveness import renew_session_lease

        if lease_session:
            renew_session_lease(db_session, row, create=True)
        if predecessor_session_id:
            predecessor = db_session.get(AgentSession, predecessor_session_id)
            if predecessor is None:
                raise ValueError(f"unknown predecessor session: {predecessor_session_id}")
            if predecessor.workspace_id != workspace.id:
                raise ValueError("predecessor and successor must belong to the same workspace")
            if predecessor.id == session_id:
                raise ValueError("a session cannot supersede itself")
            _supersede_coordination_handle(db_session, predecessor, row)
            link = db_session.get(SessionSuccessor, predecessor_session_id)
            if link is None:
                link = SessionSuccessor(predecessor_session_id=predecessor_session_id)
                db_session.add(link)
            link.successor_session_id = session_id
            link.linked_at = utc_now()
        db_session.commit()
        db_session.refresh(row)
    if predecessor_session_id:
        _cancel_open_commands(
            predecessor_session_id,
            reason=f"the Session was superseded by {session_id}",
            result="superseded",
        )
    return _session_registration_result(
        workspace,
        session_id,
        tool,
        operator_record,
        predecessor_session_id=predecessor_session_id,
        reused=False,
    )


def _session_registration_result(
    workspace: Workspace,
    session_id: str,
    tool: str,
    operator_record: OperatorRecord,
    *,
    predecessor_session_id: str | None,
    reused: bool,
) -> dict[str, Any]:
    with SessionLocal() as db_session:
        active = (
            db_session.query(Handoff)
            .filter(Handoff.workspace_id == workspace.id, Handoff.status == "active")
            .order_by(Handoff.set_at.desc(), Handoff.id.desc())
            .first()
        )
        active_handoff = (
            {
                "id": active.id,
                "title": active.title,
                "body": active.body,
                "status": active.status,
            }
            if active
            else None
        )
        lease = db_session.get(SessionLease, session_id)
        lease_expires_at = lease.lease_expires_at.isoformat() if lease else None
    # Build the discoverability welcome packet so the agent sees unread
    # mail, applicable patterns, workspace memory keys, registered-tool
    # status and indexed-source status without having to call five tools.
    # Defensive: a welcome failure must never block session start.
    #
    # Built BEFORE the ``session_start`` event so the event's metadata
    # can carry a snapshot of the offered counts. That snapshot is the
    # join key for adoption queries — e.g. "of the sessions where
    # welcome.unread_messages > 0, how many fired a read_messages event
    # within 2 minutes?" — without inventing a new telemetry table.
    try:
        from brains.control.welcome import build_welcome

        welcome = build_welcome(workspace, session_id)
    except Exception:
        welcome = None
    welcome_metadata: dict[str, Any] = {
        "tool": tool,
        "operator": operator_record["slug"],
    }
    if welcome is not None:
        tool_status = welcome.get("tool_status") or {}
        index_status = welcome.get("index_status") or {}
        welcome_metadata["welcome"] = {
            "unread_messages": int((welcome.get("unread_messages") or {}).get("count", 0)),
            "applicable_patterns": len(welcome.get("applicable_patterns") or []),
            "knowledge": int((welcome.get("knowledge") or {}).get("count", 0)),
            "relevant_memories": len(welcome.get("relevant_memories") or []),
            "tools_missing": int(tool_status.get("missing", 0)),
            "tools_unverified": int(tool_status.get("unverified", 0)),
            "index_sources": int(index_status.get("sources", 0)),
            "hints": len(welcome.get("hints") or []),
        }
    append_event(
        "session_reused" if reused else "session_start",
        f"{tool} session {'reused' if reused else 'started'}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata=welcome_metadata,
    )
    if predecessor_session_id and not reused:
        append_event(
            "session_superseded",
            f"{predecessor_session_id} -> {session_id}",
            workspace_id=workspace.id,
            session_id=session_id,
            metadata={
                "from_session_id": predecessor_session_id,
                "to_session_id": session_id,
                "automatic": True,
            },
        )
    # Best-effort: warm the code graph + embeddings in the background so the first
    # retrieval call this session makes is instant. Never blocks session start.
    try:
        from brains.context.prewarm import schedule_prewarm

        schedule_prewarm(workspace.path)
    except Exception:
        pass
    return {
        "session_id": session_id,
        "workspace": workspace.slug,
        "operator": operator_record["slug"],
        "active_handoff": active_handoff,
        "welcome": welcome,
        "predecessor_session_id": predecessor_session_id,
        "lease_expires_at": lease_expires_at,
        "reused": reused,
    }


def heartbeat_session(session_id: str, *, allow_ended: bool = False) -> dict[str, Any]:
    """Renew a PID-less coordination Session without writing a journal event."""
    from brains.control.session_liveness import renew_session_lease

    init_db()
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        if row is None:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}")
        if row.ended_at is not None and not allow_ended:
            raise ValueError(f"session already ended: {session_id}")
        lease = session.get(SessionLease, row.id)
        if row.ended_at is None:
            lease = renew_session_lease(session, row)
        if row.ended_at is None and lease is None:
            row.last_activity_at = utc_now()
        session.commit()
        return {
            "session_id": row.id,
            "state": row.state,
            "lease_expires_at": lease.lease_expires_at.isoformat() if lease else None,
        }


def link_session_successor(from_session_id: str, to_session_id: str) -> dict[str, Any]:
    """Explicitly link a predecessor handle to one same-workspace successor."""
    if from_session_id == to_session_id:
        raise ValueError("a session cannot supersede itself")
    init_db()
    with SessionLocal() as session:
        predecessor = session.get(AgentSession, from_session_id)
        successor = session.get(AgentSession, to_session_id)
        if predecessor is None:
            raise ValueError(f"unknown predecessor session: {from_session_id}")
        if successor is None:
            raise ValueError(f"unknown successor session: {to_session_id}")
        if predecessor.workspace_id != successor.workspace_id:
            raise ValueError("predecessor and successor must belong to the same workspace")
        require_live_session(session, to_session_id, action="link_session_successor")
        _supersede_coordination_handle(session, predecessor, successor)
        link = session.get(SessionSuccessor, from_session_id)
        if link is None:
            link = SessionSuccessor(predecessor_session_id=from_session_id)
            session.add(link)
        link.successor_session_id = successor.id
        link.linked_at = utc_now()
        session.commit()
    _cancel_open_commands(
        from_session_id,
        reason=f"the Session was superseded by {to_session_id}",
        result="superseded",
    )
    append_event(
        "session_superseded",
        f"{from_session_id} -> {to_session_id}",
        workspace_id=successor.workspace_id,
        session_id=to_session_id,
        metadata={"from_session_id": from_session_id, "to_session_id": to_session_id},
    )
    return {"from_session_id": from_session_id, "to_session_id": to_session_id, "linked": True}


def predecessor_session_ids(session, session_id: str) -> list[str]:
    """Transitive predecessor handles, bounded and cycle-safe."""
    found: list[str] = []
    frontier = [session_id]
    while frontier and len(found) < 20:
        current = frontier.pop(0)
        from brains.storage.models import SessionSuccessor

        rows = (
            session.query(SessionSuccessor.predecessor_session_id)
            .filter(SessionSuccessor.successor_session_id == current)
            .all()
        )
        for (predecessor_id,) in rows:
            if predecessor_id in found or predecessor_id == session_id:
                continue
            found.append(predecessor_id)
            frontier.append(predecessor_id)
    return found


def _runtime_machine_id(session, runtime_id: int | None) -> str | None:
    """The machine a Runtime actually runs on, where the row can be read.

    A spawn Session is created by the *hub* process, so stamping it with the
    hub's machine would record a box the agent never runs on - and every
    surface that reads the stamp (the zombie reaper, reconciliation, command
    routing) would then be reasoning about the wrong machine. The Runtime's
    own registration is the authority.
    """
    if runtime_id is None:
        return None
    from brains.storage.models import Runtime

    row = session.get(Runtime, runtime_id)
    return row.machine_id if row is not None and row.machine_id else None


def open_spawn_session(
    *,
    persona_id: int,
    tool: str,
    issue_id: int | None = None,
    runtime_id: int | None = None,
    workspace_path: str | None = None,
    org_id: int | None = None,
    operator: str | None = None,
) -> dict:
    """Create a pending ``agent_sessions`` row for a remote persona spawn (F0.1).

    Unlike :func:`start_session` this stamps the WS2 link columns
    ``{persona_id, issue_id, runtime_id}``, records **no** pid (the agent runs on
    a remote runtime, not this process), and reuses/registers a workspace so the
    NOT NULL ``workspace_id`` FK is satisfied. The companion
    ``control.assignments.enqueue_spawn`` queues the order the daemon pulls.
    """
    from brains.control.operators import resolve_current_operator

    init_db()
    operator_record = resolve_current_operator(operator=operator)
    workspace = register_workspace(workspace_path or os.getcwd(), org_id=org_id)
    if org_id is not None and workspace.org_id != org_id:
        raise ValueError(
            f"workspace {workspace.slug!r} belongs to another Org and cannot host this Session"
        )
    session_id = f"ses_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db_session:
        row = AgentSession(
            id=session_id,
            workspace_id=workspace.id,
            tool=tool,
            pid=None,
            machine_id=_runtime_machine_id(db_session, runtime_id) or current_machine_id(),
            created_by_operator_id=operator_record["id"],
            issue_id=issue_id,
            persona_id=persona_id,
            runtime_id=runtime_id,
            state="spawning",
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        result = _agent_session_to_dict(row)
    append_event(
        "spawn_session_opened",
        f"spawn session {session_id} for persona {persona_id}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={
            "persona_id": persona_id,
            "issue_id": issue_id,
            "runtime_id": runtime_id,
        },
    )
    return result


def _agent_session_to_dict(row: AgentSession) -> dict:
    started = row.started_at
    ended = row.ended_at
    duration_seconds = None
    if started is not None and ended is not None:
        duration_seconds = max(0.0, (ended - started).total_seconds())
    return {
        "id": row.id,
        "status": (
            "ended"
            if row.ended_at is not None
            else (DORMANT_SESSION_STATE if row.state == DORMANT_SESSION_STATE else "running")
        ),
        # F3.2 explicit lifecycle
        # (spawning/running/dormant/blocked/completed/failed).
        "state": getattr(row, "state", None) or ("completed" if ended else "running"),
        "tool": row.tool,
        "workspace_id": row.workspace_id,
        "machine_id": row.machine_id,
        "issue_id": row.issue_id,
        "persona_id": row.persona_id,
        "runtime_id": row.runtime_id,
        "pid": row.pid,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "duration_seconds": duration_seconds,
        "summary": row.summary,
        # BL-P0-05: whether a console message can reach this Session's agent at
        # all. Declared by the launch shape rather than guessed by the console,
        # so a composer is disabled with a reason instead of accepting text
        # that would be settled ``unsupported`` a moment later.
        "message_capability": _message_capability(row.tool),
    }


def _message_capability(tool: str | None) -> dict:
    try:
        from brains.exec import session_channel

        return session_channel.message_capability(tool)
    except Exception:  # pragma: no cover - capability lookup must not break a read
        return {"supported": False, "reason": "the message capability could not be determined"}


# Public execution states. ``dormant`` is maintained by the coordination
# lease/successor lifecycle rather than accepted as an arbitrary state update.
SESSION_STATES = {
    "spawning",
    "running",
    "blocked",
    "completed",
    "failed",
}
_TERMINAL_SESSION_STATES = {"completed", "failed"}


def set_session_state(session_id: str, state: str, *, summary: str | None = None) -> dict:
    """Transition a session's explicit lifecycle state (F3.2).

    ``completed``/``failed`` stamp ``ended_at`` (so duration is computed) and a
    summary; ``blocked``/``running``/``spawning`` are non-terminal. Publishes a
    ``session.state`` event on the session topic so the console updates live.
    """
    if state not in SESSION_STATES:
        raise ValueError(f"invalid session state: {state!r}")
    init_db()
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        if row is None:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}")
        row.state = state
        if summary is not None:
            row.summary = summary
        if state in _TERMINAL_SESSION_STATES and row.ended_at is None:
            row.ended_at = utc_now()
        session.commit()
        session.refresh(row)
        result = _agent_session_to_dict(row)
    if state in _TERMINAL_SESSION_STATES:
        # The Session can no longer receive anything (BL-P0-05).
        _cancel_open_commands(
            session_id,
            reason=f"the Session reached {state} before the command was delivered",
        )
    try:
        from brains.api.realtime_publish import publish_session

        publish_session(None, "session.state", result)
    except Exception:
        pass
    return result


def list_agent_sessions(
    *,
    status: str | None = None,
    issue_id: int | None = None,
    persona_id: int | None = None,
    runtime_id: int | None = None,
    workspace_id: int | None = None,
    machine_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read ``agent_sessions`` filtered by the WS2 link columns (board/operator
    read surface). ``status`` is derived: ``running`` (``ended_at IS NULL``) or
    ``ended``."""
    init_db()
    with SessionLocal() as session:
        query = session.query(AgentSession)
        if issue_id is not None:
            query = query.filter(AgentSession.issue_id == issue_id)
        if persona_id is not None:
            query = query.filter(AgentSession.persona_id == persona_id)
        if runtime_id is not None:
            query = query.filter(AgentSession.runtime_id == runtime_id)
        if workspace_id is not None:
            query = query.filter(AgentSession.workspace_id == workspace_id)
        if machine_id is not None:
            query = query.filter(AgentSession.machine_id == machine_id)
        if status == "running":
            query = query.filter(
                AgentSession.ended_at.is_(None),
                AgentSession.state != DORMANT_SESSION_STATE,
            )
        elif status == DORMANT_SESSION_STATE:
            query = query.filter(AgentSession.state == DORMANT_SESSION_STATE)
        elif status == "ended":
            query = query.filter(AgentSession.ended_at.isnot(None))
        rows = (
            query.order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
            .limit(limit)
            .all()
        )
        return [_agent_session_to_dict(r) for r in rows]


def get_agent_session(session_id: str) -> dict | None:
    init_db()
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        return _agent_session_to_dict(row) if row is not None else None


def list_agent_session_events(session_id: str, *, limit: int = 100) -> list[dict]:
    """Recent ledger/stdout events for a session (newest first), for WS backfill."""
    from brains.storage.models import Event

    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(Event)
            .filter(Event.session_id == session_id)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "message": r.message,
                "metadata_json": r.metadata_json,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def end_session(session_id: str, summary: str = "") -> dict:
    init_db()
    with SessionLocal() as session:
        row = session.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
        if row is None:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}")
        if row.ended_at is not None:
            raise ValueError(f"session already ended: {session_id}")
        now = utc_now()
        row.ended_at = now
        row.summary = summary
        if getattr(row, "state", None) not in _TERMINAL_SESSION_STATES:
            row.state = "completed"
        workspace = session.query(Workspace).filter(Workspace.id == row.workspace_id).one()
        workspace.last_touched_at = now
        workspace.last_summary = summary
        session.commit()
        workspace_id = row.workspace_id
    append_event(
        "session_end",
        summary or "(no summary)",
        workspace_id=workspace_id,
        session_id=session_id,
    )
    # A Session that has ended can receive nothing, so its queue is settled
    # rather than left pending: an operator must not be shown a message or a
    # stop that no consumer will ever pick up (BL-P0-05).
    _cancel_open_commands(session_id, reason="the Session ended before the command was delivered")
    try:
        with SessionLocal() as session:
            workspace = session.query(Workspace).filter(Workspace.id == workspace_id).one()
            from brains.control.views import refresh_views

            refresh_views(workspace.path)
    except Exception:
        pass
    return {"ok": True, "session_id": session_id}


# --------------------------------------------------------------------------- #
# Terminal synchronisation (BL-P0-05)
# --------------------------------------------------------------------------- #

#: How long after a Session was opened a reconciliation refuses to declare it
#: unowned. A daemon registers the process a moment *after* the hub row is
#: created, so a reconciliation that ran in that window would end a Session
#: that is starting normally.
RECONCILE_GRACE_SECONDS = 90


def _cancel_open_commands(session_id: str, *, reason: str, result: str | None = None) -> None:
    try:
        from brains.control import session_commands as commands_ctl

        commands_ctl.cancel_open_for_session(
            session_id,
            reason=reason,
            result=result or commands_ctl.RESULT_SESSION_ENDED,
        )
    except Exception:  # pragma: no cover - queue settlement is best effort here
        pass


def finalize_session(
    session_id: str,
    *,
    state: str = "failed",
    summary: str,
    cancel_reason: str | None = None,
) -> dict | None:
    """Bring a Session to a terminal state exactly once, and clean up after it.

    This is the one place a Session ends for a reason other than its own
    process reporting completion - an operator stop, a Runtime that restarted
    and no longer owns the process - and it has to be safe against the Session
    finishing naturally at the same moment. The stamp is therefore a single
    conditional UPDATE on ``ended_at IS NULL``: the natural finish and the
    stop race, exactly one wins, and the loser changes nothing rather than
    overwriting a completion with a failure.

    Everything the ended Session was holding is released in the same pass -
    its Workspace claim, its in-progress Tasks, its open commands - and an
    Issue it was working is moved to ``blocked`` rather than back to ``open``,
    because re-opening it would have the daemon immediately re-spawn the work
    an operator just stopped.

    Returns the Session row when this call ended it, and ``None`` when it was
    already terminal.
    """
    if state not in _TERMINAL_SESSION_STATES:
        raise ValueError(f"a terminal state must be one of {sorted(_TERMINAL_SESSION_STATES)}")
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        row = session.get(AgentSession, session_id)
        if row is None:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}")
        issue_id = row.issue_id
        workspace_id = row.workspace_id
        # Conditional stamp: the natural finish and this call race for it.
        updated = (
            session.query(AgentSession)
            .filter(AgentSession.id == session_id, AgentSession.ended_at.is_(None))
            .update(
                {
                    "ended_at": now,
                    "state": state,
                    "summary": summary,
                },
                synchronize_session=False,
            )
        )
        if updated:
            session.query(WorkspaceClaim).filter(WorkspaceClaim.session_id == session_id).delete(
                synchronize_session=False
            )
            session.query(AgentTask).filter(
                AgentTask.claimed_by_session_id == session_id,
                AgentTask.status == "in_progress",
            ).update(
                {"status": "available", "claimed_by_session_id": None, "claimed_at": None},
                synchronize_session=False,
            )
        session.commit()
        if not updated:
            _cancel_open_commands(session_id, reason=summary)
            return None
        session.expire_all()
        result = _agent_session_to_dict(cast("AgentSession", session.get(AgentSession, session_id)))
    _cancel_open_commands(session_id, reason=cancel_reason or summary)
    if issue_id is not None:
        _block_issue_for_stopped_session(issue_id, summary, session_id)
    append_event(
        "session_finalized",
        summary,
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"state": state, "issue_id": issue_id},
    )
    try:
        from brains.api.realtime_publish import publish_session

        publish_session(None, "session.state", result)
    except Exception:  # pragma: no cover - realtime is best effort
        pass
    return result


def _block_issue_for_stopped_session(issue_id: int, summary: str, session_id: str) -> None:
    """Move an in-flight Issue to ``blocked`` when its Session was stopped.

    Returning it to ``open`` would be worse than doing nothing: the assignment
    poll would surface it again within seconds and the daemon would re-spawn
    exactly the work an operator just stopped. ``blocked`` keeps the Issue
    visible and waiting for a human instead.
    """
    try:
        from brains.control import issues as issues_ctl

        issue = issues_ctl.get_issue(issue_id)
        if issue is None or issue.get("status") != "in_progress":
            return
        issues_ctl.transition(issue_id, "blocked", session_id=session_id)
    except Exception:  # pragma: no cover - Issue sync is best effort
        pass


def reconcile_machine_sessions(
    machine_id: str | None,
    owned_session_ids: list[str] | set[str],
    *,
    runtime_id: int | None = None,
    reason: str | None = None,
    grace_seconds: int = RECONCILE_GRACE_SECONDS,
) -> list[dict]:
    """End the Sessions a consumer is recorded as running but no longer owns.

    A Runtime that restarts loses every process handle it held. The hub still
    has those Sessions marked running, and their operators still see a live
    console, so the daemon reports what it *does* own on startup and reconnect
    and everything else scoped to that consumer is brought to a terminal state
    with a truthful summary.

    The scope is the strongest binding available. Given a ``runtime_id``, that
    Runtime's Sessions are the scope whatever machine they are stamped with: a
    spawn row carries the hub's machine until the daemon opens it, and adding
    the machine as a second condition would silently exclude exactly the
    Sessions that most need reconciling. With no ``runtime_id`` the machine is
    the only binding there is, and it is used alone.

    Sessions younger than ``grace_seconds`` are left alone: a daemon opens the
    hub row a moment before it registers the process, and reconciling inside
    that window would end a Session that is starting normally.
    """
    if runtime_id is None and not machine_id:
        return []
    owned = {str(sid) for sid in owned_session_ids}
    init_db()
    cutoff = utc_now() - timedelta(seconds=max(0, grace_seconds))
    with SessionLocal() as session:
        query = session.query(AgentSession).filter(AgentSession.ended_at.is_(None))
        if runtime_id is not None:
            query = query.filter(AgentSession.runtime_id == runtime_id)
        else:
            query = query.filter(AgentSession.machine_id == machine_id)
        candidates = []
        for row in query.all():
            if row.id in owned:
                continue
            started = row.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=cutoff.tzinfo)
            if started is not None and started > cutoff:
                continue
            candidates.append(row.id)
    where = f"runtime {runtime_id}" if runtime_id is not None else f"machine {machine_id}"
    summary = reason or (
        f"reconciled on {where}: the Runtime does not own an agent process for this "
        "Session, so its outcome is unknown"
    )
    out: list[dict] = []
    for session_id in candidates:
        finalized = finalize_session(session_id, state="failed", summary=summary)
        if finalized is not None:
            out.append(finalized)
    return out


def list_sessions(workspace_path: str | None = None, limit: int = 50) -> list[dict]:
    # Layer 2 of the multi-operator model: filter by visibility BEFORE
    # we touch the DB so private workspaces never leak into the result
    # for operators without explicit membership. ``None`` = admin or
    # back-compat fallback = no filter, identical to pre-Layer-2.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(AgentSession, Workspace).join(
            Workspace, Workspace.id == AgentSession.workspace_id
        )
        if workspace_path:
            workspace = _resolve_workspace_path(session, workspace_path)
            if workspace is None:
                return []
            query = query.filter(AgentSession.workspace_id == workspace.id)
        if visible is not None:
            query = query.filter(AgentSession.workspace_id.in_(visible))
        rows = (
            query.order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": row.id,
                "workspace": workspace.slug,
                "tool": row.tool,
                "state": row.state,
                "started_at": row.started_at.isoformat(),
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "last_activity_at": (
                    row.last_activity_at.isoformat() if row.last_activity_at else None
                ),
                "summary": row.summary,
            }
            for row, workspace in rows
        ]
