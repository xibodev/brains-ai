from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import update

from brains import audit
from brains.control.common import (
    insert_coded_row_in_session,
    next_sequential_code,
    utc_now,
)
from brains.control.events import append_event
from brains.control.sessions import register_workspace
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRouting,
    GovernedAction,
    Operator,
    OrgMember,
    Persona,
    Workspace,
)

#: An approval that a governed action has spent. Distinct from ``resolved`` so
#: the single-use rule is a state transition the database can enforce with a
#: conditional update rather than a read-then-write race.
CONSUMED_STATUS = "consumed"
APPROVAL_PRIORITIES = {"p0", "p1", "p2", "p3"}


class ApprovalAuthorizationError(PermissionError):
    """The resolver may not decide this approval request.

    Raised for a Runtime credential, for an agent identity trying to resolve an
    ASK its own Persona or Session filed, and for any credential that cannot be
    bound to a human when the ASK came from a Session - the separation of duty
    that makes an approval mean something.
    """


def _session_persona_id(session, session_id: str | None) -> int | None:
    if not session_id:
        return None
    row = session.get(AgentSession, session_id)
    return row.persona_id if row is not None else None


def _record_denial(principal, code: str, reason: str, workspace_id: int | None) -> None:
    """Record a refused resolution. Best-effort: a missing record must never
    turn a denial into an approval."""
    from brains import audit as _audit

    _audit.record(
        actor=principal.describe(),
        action="approval.self_resolution_denied",
        payload={"code": code, "reason": reason},
        workspace_id=workspace_id,
    )


def _server_bound_session_ids(principal) -> frozenset[str]:
    """Session ids the *server* knows this principal is running.

    Derived from the credential (a Runtime credential names its machine, and
    the machine names its live Sessions), never from the request body.
    """
    return getattr(principal, "bound_session_ids", None) or frozenset()


def assert_resolver_allowed(
    session,
    ask: ApprovalRequest,
    principal,
    *,
    resolving_session_id: str | None = None,
) -> None:
    """Refuse a resolver that is not separated from the requester.

    The rules, in order:

    1. A **Runtime** credential can never resolve an approval. It exists to run
       work on one machine, not to authorise it.
    2. An ASK filed **by a Session** may only be resolved through a channel
       that can be bound to a human: the signed console cookie, or a local CLI
       / stdio invocation whose trust boundary is the operating-system user. A
       raw operator key presented over HTTP is refused, because an agent
       process holding a shared key presents exactly the same bytes its owner
       would. This is the fail-closed half of the rule: the caller cannot make
       itself separated simply by omitting a ``session_id``.
    3. The **Session that filed the ASK** can never resolve it. That Session is
       taken from what the server knows the credential is running
       (:func:`brains.authz.resolver.sessions_bound_to_machine`) *and* from any
       Session the caller declared. A declared id can therefore only ever add a
       denial; it is never what establishes separation.
    4. The **agent identity behind the ASK** can never resolve it: when the
       requesting Session runs a Persona, the operator that Persona is bound to
       (``personas.operator_id``) is refused, and so is any Session of that
       same Persona.

    A human operator that is not the Persona behind the request is unaffected,
    so the single-operator install where one person runs the agent and approves
    its ask from the console or the CLI keeps working. The limit of the rule is
    stated in ``docs/ARCHITECTURE.md``.
    """
    if getattr(principal, "is_runtime", False):
        _record_denial(principal, ask.code, "runtime_credential", ask.workspace_id)
        raise ApprovalAuthorizationError(f"a Runtime credential cannot resolve approval {ask.code}")

    if ask.session_id and not getattr(principal, "is_human_channel", False):
        _record_denial(principal, ask.code, "unbindable_credential", ask.workspace_id)
        raise ApprovalAuthorizationError(
            f"approval {ask.code} was requested by a Session, so it must be resolved from the "
            "console or a local CLI; a shared API key cannot be bound to a human resolver"
        )

    candidate_sessions = set(_server_bound_session_ids(principal))
    if resolving_session_id:
        candidate_sessions.add(resolving_session_id)

    if ask.session_id and ask.session_id in candidate_sessions:
        _record_denial(principal, ask.code, "same_session", ask.workspace_id)
        raise ApprovalAuthorizationError(
            f"session {ask.session_id} cannot resolve the approval it requested ({ask.code})"
        )

    requesting_persona_id = _session_persona_id(session, ask.session_id)
    if requesting_persona_id is None:
        return

    for candidate in candidate_sessions:
        if _session_persona_id(session, candidate) == requesting_persona_id:
            _record_denial(principal, ask.code, "same_persona", ask.workspace_id)
            raise ApprovalAuthorizationError(
                f"a Persona cannot resolve the approval its own Session requested ({ask.code})"
            )

    operator_id = getattr(principal, "operator_id", None)
    if operator_id is None:
        return
    persona = session.get(Persona, requesting_persona_id)
    if persona is not None and persona.operator_id == operator_id:
        _record_denial(principal, ask.code, "same_persona_operator", ask.workspace_id)
        raise ApprovalAuthorizationError(
            f"the Persona identity that requested approval {ask.code} cannot resolve it"
        )


#: Columns outside the live coded table that permanently hold a code of the
#: same series. ``governed_actions.approval_code`` is the one that matters: it
#: is unique, it is never deleted, and the ``approval_requests`` row that
#: minted it *can* be (a Workspace prune cascades into its approvals). Minting
#: from the live table alone would then hand out a code that is still bound to
#: a governed action, and the collision would only surface later - when the
#: next approval is consumed - as a unique-constraint violation on a different
#: table.
_RESERVED_CODE_COLUMNS: dict[str, tuple] = {"ASK": (GovernedAction.approval_code,)}


def _next_code(session, table, prefix: str) -> str:
    """The next ``PREFIX-NNNN`` code, from the highest suffix already taken.

    ``count() + 1`` re-mints a live code as soon as any earlier row is deleted
    (a pruned Workspace cascades into its approvals), so the sequence is
    derived from ``max(suffix)`` instead - across the live table *and* every
    column in :data:`_RESERVED_CODE_COLUMNS` that keeps a spent code forever.
    That is still racy between two writers on a shared store;
    :func:`insert_coded_row_in_session` closes the race by retrying the
    unique-index collision with a freshly computed code.
    """
    return next_sequential_code(
        session, table.code, prefix, also_reserved=_RESERVED_CODE_COLUMNS.get(prefix, ())
    )


def create_request_in_session(
    session,
    *,
    workspace_id: int | None,
    title: str,
    body: str = "",
    proposed_answer: str | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Insert one approval request inside the caller's transaction.

    :mod:`brains.govern` files the ASK and the audit entry that records it
    in one transaction, so an approval can never exist without the record
    that says why it was asked for. Two writers filing at the same instant
    can still pick the same ``ASK-NNNN``; the loser of that race retries
    inside a savepoint rather than failing the caller's transaction.

    The code is minted above the highest suffix in ``approval_requests`` *and*
    in ``governed_actions.approval_code``, so a pruned Workspace - which
    cascades away every ASK row it owned - cannot make the next ASK re-use a
    code that a permanent governed action still holds.
    """
    row = insert_coded_row_in_session(
        session,
        lambda: _next_code(session, ApprovalRequest, "ASK"),
        lambda code: ApprovalRequest(
            code=code,
            workspace_id=workspace_id,
            session_id=session_id,
            title=title,
            body=body,
            proposed_answer=proposed_answer,
            status="open",
            metadata_json=json.dumps(metadata or {}),
        ),
    )
    return row.code


def request_scope(session, code: str) -> dict | None:
    """Return the governed-action scope stored on an approval request.

    ``None`` when the request is unknown or was filed without a scope, which
    :mod:`brains.govern` treats as "not spendable" rather than "unscoped, so
    anything goes".
    """
    row = session.query(ApprovalRequest).filter(ApprovalRequest.code == code).one_or_none()
    if row is None or not row.metadata_json:
        return None
    try:
        parsed = json.loads(row.metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("kind") != "governed_action":
        return None
    return parsed


def consume_resolved_decision(session, code: str) -> bool:
    """Atomically spend one resolved approval. ``False`` if it was not ours.

    The conditional ``WHERE status = 'resolved'`` is the whole guarantee:
    exactly one caller can move the row out of ``resolved``, so two racing
    processes cannot both believe they hold the approval, and a retry after
    a crash cannot spend it twice.
    """
    result = session.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.code == code, ApprovalRequest.status == "resolved")
        .values(status=CONSUMED_STATUS)
    )
    return bool(result.rowcount == 1)


def file_decision_request(
    workspace_path: str,
    title: str,
    body: str = "",
    proposed_answer: str | None = None,
    session_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    workspace = register_workspace(workspace_path)
    init_db()
    with SessionLocal() as session:
        code = create_request_in_session(
            session,
            workspace_id=workspace.id,
            title=title,
            body=body,
            proposed_answer=proposed_answer,
            session_id=session_id,
            metadata=metadata,
        )
        session.commit()
    append_event(
        "decision_filed",
        f"{code}: {title}",
        workspace_id=workspace.id,
        session_id=session_id,
        metadata={"code": code},
    )
    try:
        from brains.control.views import refresh_views

        refresh_views(workspace.path)
    except Exception:
        pass
    # ASKs deserve email: best-effort operator copy when the mailer is
    # configured. Never blocks or fails the ask (durable row is authoritative).
    try:
        from brains.control.mailer import notify_ask

        notify_ask(code, title, workspace.slug)
    except Exception:
        pass
    return {"code": code, "status": "open", "workspace": workspace.slug}


def _routing_to_dict(
    routing: ApprovalRouting | None,
    assigned_slug: str | None = None,
    *,
    now=None,
) -> dict:
    current = now or utc_now()
    due_at = routing.due_at if routing is not None else None
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=current.tzinfo)
    return {
        "assigned_operator": assigned_slug,
        "priority": routing.priority if routing is not None else "p2",
        "due_at": due_at.isoformat() if due_at else None,
        "overdue": bool(due_at and due_at < current),
        "escalation_level": routing.escalation_level if routing is not None else 0,
        "escalation_reason": routing.escalation_reason if routing is not None else None,
        "routing_updated_at": routing.updated_at.isoformat() if routing is not None else None,
    }


def _normalize_due_at(value: datetime | str | None) -> datetime | None:
    if value is None:
        return value
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("due_at must be an ISO-8601 timestamp") from exc


def _approval_metadata(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _assert_human_router(principal) -> None:
    if getattr(principal, "is_runtime", False) or not getattr(principal, "is_human_channel", False):
        raise ApprovalAuthorizationError(
            "approval routing requires a human-bound browser or local CLI principal"
        )


def _routing_target(session, ask: ApprovalRequest, operator_slug: str | None) -> Operator | None:
    if operator_slug is None:
        return None
    target = (
        session.query(Operator).filter(Operator.slug == operator_slug.strip().lower()).one_or_none()
    )
    if target is None:
        raise ValueError(f"unknown operator: {operator_slug!r}")
    workspace = session.get(Workspace, ask.workspace_id)
    if workspace is None or workspace.org_id is None:
        raise ValueError(f"approval {ask.code} has no resolvable Org")
    if target.slug == "admin":
        return target
    member = (
        session.query(OrgMember)
        .filter(OrgMember.org_id == workspace.org_id, OrgMember.operator_id == target.id)
        .one_or_none()
    )
    if member is None:
        raise ValueError(f"operator {target.slug!r} is not a member of the approval's Org")
    return target


def route_decision(
    code: str,
    *,
    assigned_operator: str | None = None,
    clear_assignment: bool = False,
    priority: str | None = None,
    due_at: datetime | str | None = None,
    clear_due: bool = False,
    escalation_level: int | None = None,
    escalation_reason: str = "",
    increment_escalation: bool = False,
    principal=None,
) -> dict:
    """Assign or escalate one open approval through a human-bound principal."""
    if priority is not None and priority not in APPROVAL_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(APPROVAL_PRIORITIES)}")
    if escalation_level is not None and escalation_level < 0:
        raise ValueError("escalation_level must be non-negative")
    if clear_assignment and assigned_operator is not None:
        raise ValueError("assigned_operator and clear_assignment are mutually exclusive")
    if clear_due and due_at is not None:
        raise ValueError("due_at and clear_due are mutually exclusive")
    if increment_escalation and escalation_level is not None:
        raise ValueError("set escalation_level or increment it, not both")
    if increment_escalation and not escalation_reason.strip():
        raise ValueError("escalation_reason is required when escalating")
    normalized_due_at = _normalize_due_at(due_at)
    if principal is None:
        from brains.authz.resolver import resolve_local_principal

        principal = resolve_local_principal()
    _assert_human_router(principal)
    init_db()
    with SessionLocal() as session:
        ask = session.query(ApprovalRequest).filter(ApprovalRequest.code == code).one_or_none()
        if ask is None:
            raise ValueError(f"unknown decision request: {code}")
        if ask.status != "open":
            raise ValueError(f"decision request {code} is {ask.status}, not open")
        workspace = session.get(Workspace, ask.workspace_id)
        if workspace is None or workspace.org_id is None:
            raise ValueError(f"approval {code} has no resolvable Org")
        if not principal.has_capability("org.write", workspace.org_id):
            raise ApprovalAuthorizationError(f"principal cannot route approval {code}")
        from brains.authz.policy import visible_workspace_ids

        visible = visible_workspace_ids(principal)
        if visible is not None and ask.workspace_id not in visible:
            raise ApprovalAuthorizationError(f"principal cannot route approval {code}")
        routing = session.get(ApprovalRouting, ask.id)
        if (
            routing is None
            and assigned_operator is None
            and not clear_assignment
            and priority is None
            and due_at is None
            and not clear_due
            and escalation_level is None
            and not increment_escalation
            and not escalation_reason.strip()
        ):
            raise ValueError("routing update must assign, prioritize, deadline, or escalate")
        now = utc_now()
        if clear_assignment:
            target = None
        elif assigned_operator is not None:
            target = _routing_target(session, ask, assigned_operator)
        elif routing is not None and routing.assigned_operator_id is not None:
            target = session.get(Operator, routing.assigned_operator_id)
        else:
            target = None
        effective_priority = priority or (routing.priority if routing is not None else "p2")
        effective_due_at = (
            None
            if clear_due
            else (
                normalized_due_at
                if due_at is not None
                else (routing.due_at if routing is not None else None)
            )
        )
        if effective_due_at is not None and effective_due_at.tzinfo is None:
            effective_due_at = effective_due_at.replace(tzinfo=UTC)
        current_level = routing.escalation_level if routing is not None else 0
        requested_level = (
            current_level + 1
            if increment_escalation
            else (escalation_level if escalation_level is not None else current_level)
        )
        if routing is not None and requested_level < routing.escalation_level:
            raise ValueError("escalation_level cannot decrease")
        if requested_level > current_level and not escalation_reason.strip():
            raise ValueError("escalation_reason is required when increasing escalation")
        normalized_reason = escalation_reason.strip() or (
            routing.escalation_reason if routing is not None else None
        )
        existing_due_at = routing.due_at if routing is not None else None
        if existing_due_at is not None and existing_due_at.tzinfo is None:
            existing_due_at = existing_due_at.replace(tzinfo=UTC)
        duplicate = routing is not None and (
            routing.assigned_operator_id,
            routing.priority,
            existing_due_at,
            routing.escalation_level,
            routing.escalation_reason,
        ) == (
            target.id if target is not None else None,
            effective_priority,
            effective_due_at,
            requested_level,
            normalized_reason,
        )
        if not duplicate:
            if routing is None:
                routing = ApprovalRouting(approval_request_id=ask.id)
                session.add(routing)
            routing.assigned_operator_id = target.id if target is not None else None
            routing.priority = effective_priority
            routing.due_at = effective_due_at
            routing.escalation_level = requested_level
            routing.escalation_reason = normalized_reason
            routing.updated_by_operator_id = principal.operator_id
            routing.updated_at = now
            escalated = requested_level > current_level
            audit.append_in_session(
                session,
                actor=principal.describe(),
                action="approval.escalated" if escalated else "approval.routed",
                payload={
                    "code": code,
                    "assigned_operator": target.slug if target is not None else None,
                    "priority": effective_priority,
                    "due_at": effective_due_at.isoformat() if effective_due_at else None,
                    "escalation_level": requested_level,
                    "escalation_reason": normalized_reason,
                },
                workspace_id=ask.workspace_id,
            )
        else:
            escalated = False
        session.commit()
        assert routing is not None
        session.refresh(routing)
        workspace_id = ask.workspace_id
        assigned_slug = target.slug if target is not None else None
        result = {
            "code": code,
            "status": ask.status,
            **_routing_to_dict(routing, assigned_slug, now=now),
            "duplicate": duplicate,
        }
    if not duplicate:
        append_event(
            "decision_escalated" if escalated else "decision_routed",
            f"{code}: {assigned_slug or 'unassigned'} {effective_priority}",
            workspace_id=workspace_id,
            metadata={
                "code": code,
                "assigned_operator": assigned_slug,
                "priority": effective_priority,
                "escalation_level": requested_level,
            },
        )
    return result


def escalate_decision(
    code: str,
    *,
    reason: str,
    assigned_operator: str | None = None,
    due_at: datetime | str | None = None,
    principal=None,
) -> dict:
    """Atomically increment an approval's escalation level with a reason."""
    return route_decision(
        code,
        assigned_operator=assigned_operator,
        due_at=due_at,
        escalation_reason=reason,
        increment_escalation=True,
        principal=principal,
    )


def count_overdue_decisions() -> int:
    init_db()
    now = utc_now()
    with SessionLocal() as session:
        return (
            session.query(ApprovalRouting)
            .join(ApprovalRequest, ApprovalRequest.id == ApprovalRouting.approval_request_id)
            .filter(ApprovalRequest.status == "open", ApprovalRouting.due_at < now)
            .count()
        )


def list_open_decisions(workspace_path: str | None = None, limit: int = 50) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = (
            session.query(ApprovalRequest, Workspace, ApprovalRouting, Operator.slug)
            .join(Workspace, Workspace.id == ApprovalRequest.workspace_id)
            .outerjoin(ApprovalRouting, ApprovalRouting.approval_request_id == ApprovalRequest.id)
            .outerjoin(Operator, Operator.id == ApprovalRouting.assigned_operator_id)
        )
        query = query.filter(ApprovalRequest.status == "open")
        if workspace_path:
            workspace = register_workspace(workspace_path)
            query = query.filter(ApprovalRequest.workspace_id == workspace.id)
        if visible is not None:
            query = query.filter(ApprovalRequest.workspace_id.in_(visible))
        rows = (
            query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
            .limit(limit)
            .all()
        )
        result: list[dict] = []
        for row, workspace, routing, assigned_slug in rows:
            metadata = _approval_metadata(row.metadata_json)
            result.append(
                {
                    "code": row.code,
                    "workspace": workspace.slug,
                    "workspace_id": workspace.id,
                    "session_id": row.session_id,
                    "title": row.title,
                    "body": row.body,
                    "proposed_answer": row.proposed_answer,
                    "created_at": row.created_at.isoformat(),
                    "status": row.status,
                    "kind": metadata.get("kind"),
                    "metadata": metadata,
                    **_routing_to_dict(routing, assigned_slug),
                }
            )
        return result


def resolve_decision(
    code: str,
    chosen: str,
    reasoning: str = "",
    status: str = "resolved",
    *,
    principal=None,
    resolving_session_id: str | None = None,
) -> dict:
    """Resolve one approval request, with the resolver's identity bound.

    An approval is only meaningful if the actor that decides it is not the
    actor that asked for it. The rules are in :func:`assert_resolver_allowed`:

    * a Runtime credential can never resolve anything,
    * an ASK filed by a Session must be resolved from the console or a local
      CLI, because a shared API key cannot be bound to a human,
    * the Session the server knows the caller is running - not the one the
      caller declares - can never resolve its own ASK, and
    * an agent identity (an operator bound to a Persona) can never resolve an
      ASK filed by a Session of that same Persona.

    ``principal`` defaults to the principal of the current request or local
    invocation. ``resolving_session_id`` is an *additional* denial input only:
    it can never establish separation of duty, because a caller can omit it.
    Both the decision and its audit entry commit in one transaction, so a
    resolution that cannot be recorded does not happen.
    """
    if status not in {"resolved", "rejected", "deferred"}:
        raise ValueError("status must be resolved, rejected, or deferred")
    if principal is None:
        from brains.authz.resolver import resolve_local_principal

        principal = resolve_local_principal()
    init_db()
    with SessionLocal() as session:
        ask = session.query(ApprovalRequest).filter(ApprovalRequest.code == code).one_or_none()
        if ask is None:
            raise ValueError(f"unknown decision request: {code}")
        if ask.status != "open":
            raise ValueError(f"decision request {code} is {ask.status}, not open")
        assert_resolver_allowed(session, ask, principal, resolving_session_id=resolving_session_id)
        decision = insert_coded_row_in_session(
            session,
            lambda: _next_code(session, ApprovalDecision, "DEC"),
            lambda code: ApprovalDecision(
                code=code,
                approval_request_id=ask.id,
                chosen=chosen,
                reasoning=reasoning,
                metadata_json=json.dumps(
                    {
                        "resolved_by": principal.describe(),
                        "resolver_operator_id": principal.operator_id,
                        "resolver_credential": principal.credential_kind,
                        "resolver_channel": getattr(principal, "channel", None),
                        "resolving_session_id": resolving_session_id,
                    }
                ),
            ),
        )
        decision_code = decision.code
        ask.status = status
        ask.resolved_at = utc_now()
        ask.decision_id = decision.id
        workspace_id = ask.workspace_id
        session_id = ask.session_id
        audit.append_in_session(
            session,
            actor=principal.describe(),
            action="approval.resolved",
            payload={
                "code": code,
                "decision": decision_code,
                "status": status,
                "requesting_session_id": session_id,
                "resolving_session_id": resolving_session_id,
                "resolver_credential": principal.credential_kind,
                "resolver_channel": getattr(principal, "channel", None),
            },
            workspace_id=workspace_id,
        )
        session.commit()
    append_event(
        "decision_resolved",
        f"{code} -> {status}: {chosen[:120]}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={"code": code, "decision_code": decision_code, "status": status},
    )
    try:
        with SessionLocal() as session:
            workspace = session.query(Workspace).filter(Workspace.id == workspace_id).one()
            from brains.control.views import refresh_views

            refresh_views(workspace.path)
    except Exception:
        pass
    return {"code": code, "decision": decision_code, "status": status}


def list_open_requests() -> list[dict]:
    """All still-open approval requests, oldest-first. Used by the inbound bridge
    to disambiguate when more than one ask is pending."""
    init_db()
    with SessionLocal() as session:
        rows = (
            session.query(ApprovalRequest)
            .filter(ApprovalRequest.status == "open")
            .order_by(ApprovalRequest.id)
            .all()
        )
        return [{"code": r.code, "title": r.title} for r in rows]


def latest_open_request() -> str | None:
    """Return the code of the most-recently filed still-open approval request.

    Used by the inbound bridge to route a bare freeform answer (e.g. a numbered
    choice texted back over WhatsApp) to the single open ask when no code is given.
    """
    init_db()
    with SessionLocal() as session:
        ask = (
            session.query(ApprovalRequest)
            .filter(ApprovalRequest.status == "open")
            .order_by(ApprovalRequest.id.desc())
            .first()
        )
        return ask.code if ask else None


def get_decision(code: str) -> dict | None:
    """Return the current state of one approval request by code.

    ``{code, status, title, chosen, reasoning}`` — ``status`` is ``open`` until
    resolved, then ``resolved`` | ``rejected`` | ``deferred``; ``chosen`` /
    ``reasoning`` carry the operator's decision once made. Used by the action-gate
    poll loop. Returns ``None`` for an unknown code.
    """
    init_db()
    with SessionLocal() as session:
        ask = session.query(ApprovalRequest).filter(ApprovalRequest.code == code).one_or_none()
        if ask is None:
            return None
        chosen = None
        reasoning = None
        if ask.decision_id is not None:
            dec = session.get(ApprovalDecision, ask.decision_id)
            if dec is not None:
                chosen = dec.chosen
                reasoning = dec.reasoning
        routing = session.get(ApprovalRouting, ask.id)
        assigned_slug = None
        if routing is not None and routing.assigned_operator_id is not None:
            assigned_slug = (
                session.query(Operator.slug)
                .filter(Operator.id == routing.assigned_operator_id)
                .scalar()
            )
        return {
            "code": ask.code,
            "status": ask.status,
            "title": ask.title,
            "chosen": chosen,
            "reasoning": reasoning,
            **_routing_to_dict(routing, assigned_slug),
        }
