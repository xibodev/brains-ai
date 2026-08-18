from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime

from brains.control.common import utc_now
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.control.tasks import create_task
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import RecurringRun, RecurringTaskDefinition, Workspace

# Env flag that must be ``"1"`` for ``_auto_spawn`` to actually launch a
# subprocess. Default-off so dev environments never accidentally fork agent
# CLIs they don't intend to drive.
SPAWN_ENV_VAR = "BRAINS_ALLOW_RECURRING_SPAWN"

#: The only schedule grammar the engine understands (AC-F10-02): the literals
#: ``manual``/``hourly``/``daily``, or ``every:<N><s|m|h|d>``. This is
#: deliberately not cron syntax - :func:`brains.mcp.server._is_due` is the
#: single place that *evaluates* a schedule against this grammar; this is the
#: single place that *validates* one at create time, so an unsupported
#: expression (including a real cron string) is refused up front instead of
#: silently never firing.
_SCHEDULE_LITERALS = {"manual", "hourly", "daily"}
_SCHEDULE_EVERY = re.compile(r"^every:(\d+)([smhd])$", re.IGNORECASE)
_NO_FIRE_CLAIM = object()


class RecurringFireAlreadyClaimed(RuntimeError):
    """A concurrent scheduler already claimed the observed due occurrence."""


def is_valid_schedule(expr: str | None) -> bool:
    """``True`` when ``expr`` matches the supported schedule grammar."""
    value = (expr or "").strip().lower()
    if value in _SCHEDULE_LITERALS:
        return True
    match = _SCHEDULE_EVERY.match(value)
    return bool(match and int(match.group(1)) > 0)


def _render_template(value: str | None, now: datetime) -> str:
    if not value:
        return ""
    return value.replace("{date}", now.date().isoformat())


def _recurring_to_dict(row: RecurringTaskDefinition, workspace_slug: str | None = None) -> dict:
    return {
        "name": row.name,
        "workspace": workspace_slug,
        "title_template": row.title_template,
        "body_template": row.body_template,
        "priority": row.priority,
        "tags": row.tags,
        "cron_expr": row.cron_expr,
        "enabled": bool(row.enabled),
        "last_fired_at": row.last_fired_at.isoformat() if row.last_fired_at else None,
        "created_by_session_id": row.created_by_session_id,
        "created_at": row.created_at.isoformat(),
        "spawn_tool": row.spawn_tool,
        "spawn_args": row.spawn_args,
        "spawn_prompt": row.spawn_prompt,
        "squad": getattr(row, "squad", None),
    }


def _validate_squad(session, workspace_id: int, squad_slug: str) -> None:
    """Ensure a squad with ``squad_slug`` exists in the workspace before binding
    a recurring task to it — fail fast at create time, not at fire time."""
    from brains.storage.models import Squad

    exists = (
        session.query(Squad)
        .filter(Squad.workspace_id == workspace_id, Squad.slug == squad_slug)
        .one_or_none()
    )
    if exists is None:
        raise ValueError(f"unknown squad in this workspace: {squad_slug!r}")


def create_recurring_task(
    workspace_path: str,
    name: str,
    title_template: str,
    *,
    body_template: str = "",
    priority: str = "p2",
    tags: str = "",
    cron_expr: str = "manual",
    session_id: str | None = None,
    spawn_tool: str | None = None,
    spawn_args: str | list | None = None,
    spawn_prompt: str | None = None,
    squad: str | None = None,
) -> dict:
    if not is_valid_schedule(cron_expr):
        raise ValueError(
            f"unsupported schedule {cron_expr!r}: use 'manual', 'hourly', 'daily', or "
            "'every:<N><s|m|h|d>' (cron syntax is not supported)"
        )
    # ``spawn_args`` accepts either a JSON string or a list (which we
    # serialize). Stored as TEXT so callers can introspect it cheaply.
    if isinstance(spawn_args, list):
        spawn_args_serialized: str | None = json.dumps(spawn_args)
    elif isinstance(spawn_args, str) and spawn_args.strip():
        # Validate it parses as a JSON array — fail fast instead of at fire
        # time.
        parsed = json.loads(spawn_args)
        if not isinstance(parsed, list):
            raise ValueError("spawn_args must be a JSON array")
        spawn_args_serialized = spawn_args
    else:
        spawn_args_serialized = None
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        existing = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one_or_none()
        )
        if existing is not None:
            raise ValueError(f"recurring task already exists: {name}")
        if squad:
            _validate_squad(session, workspace.id, squad)
        row = RecurringTaskDefinition(
            name=name,
            workspace_id=workspace.id,
            title_template=title_template,
            body_template=body_template or None,
            priority=priority,
            tags=tags or None,
            cron_expr=cron_expr,
            enabled=1,
            created_by_session_id=session_id,
            spawn_tool=spawn_tool or None,
            spawn_args=spawn_args_serialized,
            spawn_prompt=spawn_prompt or None,
            squad=squad or None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _recurring_to_dict(row, workspace.slug)
    append_event(
        "recurring_task_created",
        f"{name}: {title_template}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"name": name, "cron_expr": cron_expr, "spawn_tool": spawn_tool},
    )
    return result


def list_recurring_tasks(
    workspace_path: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(RecurringTaskDefinition, Workspace).join(
            Workspace, Workspace.id == RecurringTaskDefinition.workspace_id
        )
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(RecurringTaskDefinition.workspace_id == workspace.id)
        if visible is not None:
            query = query.filter(RecurringTaskDefinition.workspace_id.in_(visible))
        if enabled is not None:
            query = query.filter(RecurringTaskDefinition.enabled == (1 if enabled else 0))
        rows = query.order_by(RecurringTaskDefinition.name.asc()).limit(limit).all()
        return [_recurring_to_dict(row, workspace.slug) for row, workspace in rows]


def set_recurring_enabled(name: str, enabled: bool) -> dict:
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"unknown recurring task: {name}")
        row.enabled = 1 if enabled else 0
        workspace = session.query(Workspace).filter(Workspace.id == row.workspace_id).one()
        session.commit()
        result = _recurring_to_dict(row, workspace.slug)
        workspace_id = row.workspace_id
    append_event(
        "recurring_task_enabled" if enabled else "recurring_task_disabled",
        f"{name}: {'enabled' if enabled else 'disabled'}",
        workspace_id=workspace_id,
        metadata={"name": name, "enabled": enabled},
    )
    return result


def _auto_spawn(
    row: RecurringTaskDefinition,
    workspace_path: str,
    task_code: str,
    rendered_title: str,
    rendered_body: str,
    *,
    session_id: str | None = None,
    workspace_id: int | None = None,
) -> dict:
    """Launch a headless agent CLI for a freshly-fired recurring task.

    Returns a status dict, never raises. The return value is what callers
    surface in the ``auto_spawn`` field of ``fire_recurring_task``'s
    payload, so callers can introspect why a spawn did not happen without
    having to interpret exceptions.

    Gating: two independent conditions must hold. The environment variable
    ``BRAINS_ALLOW_RECURRING_SPAWN`` must be ``"1"`` (so dev and CI never
    fork agent CLIs by accident), *and* the launch must pass
    :mod:`brains.exec.guard` - the same classification, approval and audit
    path a manual command takes. This used to call ``subprocess.Popen``
    directly, which is exactly the gate bypass BL-P0-03/BL-P0-04 name.

    The spawn does not block on a human: an outward-tier launch files its
    approval and returns ``pending`` rather than holding a scheduled fire
    open for the length of the approval window.
    """
    if not row.spawn_tool:
        return {"status": "skipped", "reason": "no spawn_tool configured"}
    if os.environ.get(SPAWN_ENV_VAR) != "1":
        return {
            "status": "skipped",
            "reason": f"{SPAWN_ENV_VAR}!=1 (auto-spawn disabled)",
        }
    exe = shutil.which(row.spawn_tool)
    if not exe:
        return {
            "status": "skipped",
            "reason": f"tool '{row.spawn_tool}' not found on PATH",
        }

    try:
        extra_args = json.loads(row.spawn_args) if row.spawn_args else []
    except json.JSONDecodeError as exc:
        return {"status": "error", "reason": f"invalid spawn_args JSON: {exc}"}
    if not isinstance(extra_args, list):
        return {"status": "error", "reason": "spawn_args must be a JSON array"}

    prompt = row.spawn_prompt or f"Execute task {task_code}: {rendered_title}"
    if rendered_body:
        prompt = f"{prompt}\n\nDetails:\n{rendered_body}"

    cmd = [exe, *[str(a) for a in extra_args], prompt]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    from brains.exec.guard import spawn as guarded_spawn

    try:
        launched = guarded_spawn(
            cmd,
            actor=f"recurring:{row.name}",
            action="recurring.spawn",
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            session_id=session_id,
            cwd=workspace_path,
            creationflags=creationflags,
            idempotency_key=f"recurring.spawn:{task_code}",
            wait_for_approval=False,
        )
    except OSError as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a refusal is a status, not a crash
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    if not launched.allowed:
        return {
            "status": "denied" if launched.status != "pending" else "pending",
            "reason": launched.reason or launched.status,
            "action_id": launched.action_id,
            "approval_code": launched.approval_code,
            "tool": row.spawn_tool,
            "task_code": task_code,
        }
    return {
        "status": "spawned",
        "pid": launched.pid,
        "tool": row.spawn_tool,
        "task_code": task_code,
        "action_id": launched.action_id,
    }


def fire_recurring_task(
    name: str,
    session_id: str | None = None,
    source: str = "manual",
    trigger_payload: dict | None = None,
    expected_last_fired_at: datetime | None | object = _NO_FIRE_CLAIM,
) -> dict:
    """Fire one recurring definition: record it, mint the task, gate the spawn.

    The fire is recorded *before* the work exists. Advancing ``last_fired_at``,
    writing the durable ``recurring_runs`` row and appending the
    ``recurring.fired`` audit entry all happen in one transaction, so a fire
    that cannot be recorded does not happen at all - and the schedule is not
    advanced by a fire nobody can account for.
    """
    fired_at = datetime.now(UTC)
    init_db()
    with SessionLocal() as session:
        row = (
            session.query(RecurringTaskDefinition)
            .filter(RecurringTaskDefinition.name == name)
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"unknown recurring task: {name}")
        if not row.enabled:
            raise ValueError(f"recurring task is disabled: {name}")
        workspace = session.query(Workspace).filter(Workspace.id == row.workspace_id).one()
        rendered_title = _render_template(row.title_template, fired_at)
        rendered_body = _render_template(row.body_template, fired_at)
        priority = row.priority
        tags = row.tags or ""
        squad_slug = getattr(row, "squad", None)
        # If routed to a squad, ensure the fired task carries the squad tag so it
        # lands as the squad's work (the leader then delegates).
        if squad_slug:
            squad_tag = f"squad:{squad_slug}"
            if squad_tag not in tags:
                tags = f"{tags},{squad_tag}" if tags else squad_tag
        workspace_path = workspace.path
        workspace_id = row.workspace_id
        # Capture spawn config into a detached snapshot so we don't keep the
        # SQLAlchemy row attached after the session closes.
        spawn_snapshot = RecurringTaskDefinition(
            name=row.name,
            spawn_tool=row.spawn_tool,
            spawn_args=row.spawn_args,
            spawn_prompt=row.spawn_prompt,
            workspace_id=row.workspace_id,
            title_template=row.title_template,
        )

    run_id = _record_fire(
        name,
        workspace_id=workspace_id,
        source=source,
        fired_at=fired_at,
        session_id=session_id,
        trigger_payload=trigger_payload,
        squad=squad_slug,
        expected_last_fired_at=expected_last_fired_at,
    )

    task = create_task(
        workspace_path,
        title=rendered_title,
        body=rendered_body,
        priority=priority,
        tags=tags,
        session_id=session_id,
    )
    _link_run_task(run_id, task["code"])
    auto_spawn_result = _auto_spawn(
        spawn_snapshot,
        workspace_path=workspace_path,
        task_code=task["code"],
        rendered_title=rendered_title,
        rendered_body=rendered_body,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    append_event(
        "recurring_task_fired",
        f"{name} -> {task['code']}" + (f" (squad @{squad_slug})" if squad_slug else ""),
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={
            "name": name,
            "task_code": task["code"],
            "auto_spawn": auto_spawn_result.get("status"),
            "squad": squad_slug,
        },
    )
    squad_brief = None
    if squad_slug:
        try:
            from brains.control.squads import _leader_brief
            from brains.control.squads import roster as _squad_roster

            squad_brief = _leader_brief(_squad_roster(workspace_path, squad_slug), task)
        except Exception:
            squad_brief = None
    return {
        "name": name,
        "task": task,
        "fired_at": utc_now().isoformat(),
        "auto_spawn": auto_spawn_result,
        "squad": squad_slug,
        "brief": squad_brief,
        "run_id": run_id,
    }


def _record_fire(
    definition_name: str,
    *,
    workspace_id: int | None,
    source: str,
    fired_at: datetime,
    session_id: str | None,
    trigger_payload: dict | None,
    squad: str | None,
    expected_last_fired_at: datetime | None | object = _NO_FIRE_CLAIM,
) -> int:
    """Advance the schedule, write the run row, and audit it in one transaction.

    Raises :class:`~brains.audit.AuditWriteError` when the record cannot be
    written, which aborts the fire before any task or agent process exists.
    """
    from brains.audit import append_in_session

    normalized_source = source if source in {"schedule", "manual", "webhook"} else "manual"
    with SessionLocal() as session:
        if expected_last_fired_at is _NO_FIRE_CLAIM:
            definition = (
                session.query(RecurringTaskDefinition)
                .filter(RecurringTaskDefinition.name == definition_name)
                .one()
            )
            definition.last_fired_at = fired_at
        else:
            claim = session.query(RecurringTaskDefinition).filter(
                RecurringTaskDefinition.name == definition_name,
                RecurringTaskDefinition.enabled.is_(True),
            )
            if expected_last_fired_at is None:
                claim = claim.filter(RecurringTaskDefinition.last_fired_at.is_(None))
            else:
                claim = claim.filter(
                    RecurringTaskDefinition.last_fired_at == expected_last_fired_at
                )
            claimed = claim.update(
                {RecurringTaskDefinition.last_fired_at: fired_at},
                synchronize_session=False,
            )
            if claimed != 1:
                session.rollback()
                raise RecurringFireAlreadyClaimed(
                    f"recurring task {definition_name!r} was claimed by another scheduler"
                )
        run = RecurringRun(
            definition_name=definition_name,
            workspace_id=workspace_id,
            source=normalized_source,
            status="created",
            task_code=None,
            trigger_payload=json.dumps(trigger_payload) if trigger_payload else None,
            failure_reason=None,
        )
        session.add(run)
        session.flush()
        append_in_session(
            session,
            actor=f"recurring:{definition_name}",
            action="recurring.fired",
            payload={
                "definition": definition_name,
                "run_id": run.id,
                "source": normalized_source,
                "fired_at": fired_at.isoformat(),
                "session_id": session_id,
                "squad": squad,
            },
            workspace_id=workspace_id,
        )
        session.commit()
        return int(run.id)


def _link_run_task(run_id: int, task_code: str) -> None:
    """Attach the minted task code to its already-recorded run."""
    with SessionLocal() as session:
        run = session.get(RecurringRun, run_id)
        if run is None:
            return
        run.task_code = task_code
        session.commit()


def list_recurring_runs(name: str | None = None, limit: int = 50) -> list[dict]:
    """Return the most recent recurring-task runs (audit trail).

    Filtered to a single definition when ``name`` is given, newest first.
    """
    init_db()
    with SessionLocal() as session:
        query = session.query(RecurringRun)
        if name:
            query = query.filter(RecurringRun.definition_name == name)
        rows = query.order_by(RecurringRun.created_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "definition_name": r.definition_name,
                "source": r.source,
                "status": r.status,
                "task_code": r.task_code,
                "failure_reason": r.failure_reason,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Org-scoped autopilots (F10) — the console surfaces recurring tasks as
# org-scoped "autopilots". Reuses the recurring-task engine + gate.
# --------------------------------------------------------------------------- #


def _org_workspace_path(org_id: int | None) -> str:
    """Resolve (or auto-provision) a workspace for the org so an autopilot has a
    home (mirrors the pods pattern)."""
    import os
    import tempfile

    init_db()
    with SessionLocal() as session:
        q = session.query(Workspace).filter(Workspace.status == "active")
        q = (
            q.filter(Workspace.org_id.is_(None))
            if org_id is None
            else q.filter(Workspace.org_id == org_id)
        )
        ws = q.order_by(Workspace.id).first()
        if ws is not None:
            return ws.path
    path = os.path.join(tempfile.gettempdir(), f"brains-org-{org_id or 'default'}-autopilots")
    ws = register_workspace(path, name=f"org-{org_id or 'default'}-autopilots")
    if org_id is not None:
        with SessionLocal() as session:
            row = session.get(Workspace, ws.id)
            if row is not None:
                row.org_id = org_id
                session.commit()
    return ws.path


def create_autopilot(
    org_id: int | None,
    name: str,
    title_template: str,
    *,
    cron_expr: str = "manual",
    spawn_tool: str | None = None,
    spawn_prompt: str | None = None,
    body_template: str = "",
) -> dict:
    """Create an org-scoped autopilot (a recurring task). Schedule is a cron
    expr or ``manual`` (fire on demand)."""
    ws_path = _org_workspace_path(org_id)
    return create_recurring_task(
        ws_path,
        name,
        title_template,
        body_template=body_template,
        cron_expr=cron_expr,
        spawn_tool=spawn_tool,
        spawn_prompt=spawn_prompt,
    )
