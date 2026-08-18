from __future__ import annotations

import json

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
    GovernedAction,
    Persona,
    Workspace,
)

#: An approval that a governed action has spent. Distinct from ``resolved`` so
#: the single-use rule is a state transition the database can enforce with a
#: conditional update rather than a read-then-write race.
CONSUMED_STATUS = "consumed"


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
    return {"code": code, "status": "open", "workspace": workspace.slug}


def list_open_decisions(workspace_path: str | None = None, limit: int = 50) -> list[dict]:
    # Layer 2 visibility filter — see ``brains.control.memberships``.
    from brains.control.memberships import visible_workspace_ids_for_current

    visible = visible_workspace_ids_for_current()
    init_db()
    with SessionLocal() as session:
        query = session.query(ApprovalRequest, Workspace).join(
            Workspace, Workspace.id == ApprovalRequest.workspace_id
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
        return [
            {
                "code": row.code,
                "workspace": workspace.slug,
                "title": row.title,
                "body": row.body,
                "proposed_answer": row.proposed_answer,
                "created_at": row.created_at.isoformat(),
                "status": row.status,
            }
            for row, workspace in rows
        ]


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
        return {
            "code": ask.code,
            "status": ask.status,
            "title": ask.title,
            "chosen": chosen,
            "reasoning": reasoning,
        }
