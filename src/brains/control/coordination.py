"""Local, existing-peer deliberation, without workers or outward effects.

Blinding is a response filter within this API, not a security sandbox: an operator
can act as another Session they own and can read their local database. Tool names
come from stored Sessions; model labels are declarations, never verified routing.
Context and links are inert snapshots/references and are never fetched.

Operator controls use the supplied Principal, never a borrowed Session. Human
creation records agreement without a peer acknowledgement; all selected agents
and the explicit result owner must acknowledge. Operator observers see no initial
bodies until initial_closed, even when a contributing Session belongs to them.

Versions are immutable specifications. Revisions are monotonic across each code's
versions, starting at zero on version 1. Replacement cancels the old version at
r + 1 and creates the new version at r + 2 in the same transaction, so its exact
latest-revision fence cannot collide with a historical revision.
Accept/advance/submit/cancel also require the exact latest version.
Stale version/revision retries fail even with a known contribution key; read back
before retrying. Current-revision identical retries are no-ops (no event or lease
renewal). Creation-key retries
return the membership-filtered latest version, without changing its deadline.

Only an explicit advance closes initial collection, then each discussion round.
Final synthesis cannot remove original dissent: snapshots mechanically preserve
every nonempty dissent as unresolved, including after completion. No resolution
workflow or automatic expiry job is supplied. Expiry is observed, not settlement.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func

from brains.authz.policy import require_workspace_capability
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.authz.resolver import resolve_local_principal
from brains.control.common import utc_now
from brains.control.events import TAXONOMY_VERSION, classify_event_kind
from brains.control.sessions import _lock_session_lifecycle, require_live_session
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    CoordinationContribution,
    CoordinationProposal,
    Event,
    EventContext,
    Operator,
    Org,
    Workspace,
    WorkspaceAlias,
    WorkspaceMembership,
)

SPEC_LIMIT = 32 * 1024
PAYLOAD_LIMIT = 64 * 1024
MAX_DEADLINE_DAYS = 30
_UNAVAILABLE = "unknown or unavailable coordination"
_OPEN = ("planned", "accepted", "collecting", "discussing")


def _text(value, name: str, maximum: int, *, required: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if size > maximum or "\x00" in value or (required and not value.strip()):
        raise ValueError(f"{name} must be bounded non-NUL text (maximum {maximum} UTF-8 bytes)")
    return value


def _identifier(value, name: str = "session_id") -> str:
    _text(value, name, 32, required=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{name} must be a safe Session identifier")
    return value


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _deadline(value) -> datetime:
    _text(value, "deadline", 64, required=True)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive deadline")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("deadline must be an aware ISO datetime") from exc


def _refs(value, name: str) -> list:
    if type(value) is not list or len(value) > 32:
        raise ValueError(f"{name} must be a list of at most 32 strings")
    return [_text(item, name, 2048, required=True) for item in value]


def _specification(value: dict, requester: str | None) -> dict:
    allowed = {
        "version",
        "objective",
        "participants",
        "context",
        "evidence_expectations",
        "deadline",
        "discussion_rounds",
        "exchange_mode",
        "max_turns_per_peer",
        "result_owner_session_id",
        "links",
    }
    if type(value) is not dict or any(type(k) is not str or k not in allowed for k in value):
        raise ValueError("specification must be a version-1 object with only allowed fields")
    if requester is None and not value.get("result_owner_session_id"):
        raise ValueError("operator proposals require an explicit result_owner_session_id")
    exchange_mode = value.get("exchange_mode", "rounds")
    if exchange_mode not in ("rounds", "threaded"):
        raise ValueError("exchange_mode must be 'rounds' or 'threaded'")
    discussion_rounds = _integer(value.get("discussion_rounds", 1), "discussion_rounds", 0, 3)
    max_turns = _integer(
        value.get("max_turns_per_peer", discussion_rounds or 3),
        "max_turns_per_peer",
        1,
        3,
    )
    spec = {
        "version": _integer(value.get("version", 1), "version", 1, 1),
        "objective": _text(value.get("objective"), "objective", SPEC_LIMIT, required=True),
        "context": _text(value.get("context"), "context", SPEC_LIMIT),
        "evidence_expectations": _text(
            value.get("evidence_expectations"), "evidence_expectations", SPEC_LIMIT
        ),
        "discussion_rounds": discussion_rounds,
        "exchange_mode": exchange_mode,
        "max_turns_per_peer": max_turns,
        "result_owner_session_id": _identifier(value.get("result_owner_session_id", requester)),
        "links": _refs(value.get("links", []), "links"),
    }
    peers = value.get("participants")
    if type(peers) is not list or not 2 <= len(peers) <= 8:
        raise ValueError("participants must contain 2..8 existing Sessions")
    participants = []
    for peer in peers:
        if type(peer) is not dict or set(peer) != {"session_id", "model"}:
            raise ValueError("each participant requires session_id and model (string or null)")
        model = peer["model"]
        if model is not None:
            _text(model, "model", 128, required=True)
        participants.append({"session_id": _identifier(peer["session_id"]), "model": model})
    if len({p["session_id"] for p in participants}) != len(participants):
        raise ValueError("participants must be unique")
    spec["participants"] = sorted(participants, key=lambda p: p["session_id"])
    if "deadline" in value:
        spec["deadline"] = _deadline(value["deadline"]).isoformat()
    _text(_json(spec), "specification", SPEC_LIMIT)
    return spec


def _payload(kind: str, value: dict) -> str:
    fields = (
        {"summary", "evidence"}
        if kind == "final"
        else {"findings", "evidence", "uncertainty", "dissent"}
    )
    optional = {"consensus", "open_questions", "dissent"} if kind == "final" else {"clarifications"}
    if type(value) is not dict or not fields <= value.keys() or value.keys() - fields - optional:
        raise ValueError("payload must contain the required structured fields only")
    result: dict = {
        field: _text(value[field], field, PAYLOAD_LIMIT, required=field == "evidence")
        for field in fields
    }
    if kind == "final":
        for opt in ("consensus", "open_questions", "dissent"):
            if opt in value:
                if isinstance(value[opt], list):
                    result[opt] = _refs(value[opt], opt)
                else:
                    result[opt] = _text(value[opt], opt, PAYLOAD_LIMIT)
    elif "clarifications" in value:
        result["clarifications"] = _refs(value["clarifications"], "clarifications")
    encoded = _json(result)
    _text(encoded, "payload", PAYLOAD_LIMIT)
    return encoded


def _visible(session, principal, workspace: Workspace) -> bool:
    if principal.is_bootstrap_admin:
        return True
    org_id = workspace.org_id
    if org_id is None:
        org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
    return org_id in (principal.visible_org_ids() or set()) and (
        workspace.visibility != "private"
        or session.query(WorkspaceMembership.id)
        .filter(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.operator_id == principal.operator_id,
        )
        .first()
        is not None
    )


@contextmanager
def _transaction(session_id: str, *, write: bool):
    principal = resolve_local_principal()
    if not principal.is_operator or principal.operator_id is None:
        raise ValueError(_UNAVAILABLE)
    _identifier(session_id)
    init_db()
    with SessionLocal() as session:
        if write:
            _lock_session_lifecycle(session, session_id)
            session.expire_all()
        elif session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN")
        actor = session.get(AgentSession, session_id)
        workspace = session.get(Workspace, actor.workspace_id) if actor else None
        if (
            actor is None
            or workspace is None
            or session.get(Operator, principal.operator_id) is None
            or actor.created_by_operator_id != principal.operator_id
            or not _visible(session, principal, workspace)
        ):
            raise ValueError(_UNAVAILABLE)
        # No attribution-based lease renewal before authorization or on read/no-op.
        require_live_session(session, actor.id, action="coordination", renew_lease=False)
        yield session, actor, workspace, principal


def _workspace_path(session, workspace: Workspace, path: str) -> None:
    _text(path, "workspace_path", 1024, required=True)
    if (
        path != workspace.path
        and session.query(WorkspaceAlias.id)
        .filter(
            WorkspaceAlias.workspace_id == workspace.id,
            WorkspaceAlias.path == path,
        )
        .first()
        is None
    ):
        raise ValueError(_UNAVAILABLE)


def _participants(row: CoordinationProposal) -> set[str]:
    return {peer["session_id"] for peer in json.loads(row.specification_json)["participants"]}


def _members(row: CoordinationProposal) -> set[str]:
    return _participants(row) | {
        ident
        for ident in (row.creator_session_id, row.result_owner_session_id)
        if ident is not None
    }


def _access(row, actor, workspace, principal) -> None:
    if (
        row is None
        or row.workspace_id != workspace.id
        or row.creator_operator_id != principal.operator_id
        or (actor is not None and actor.id not in _members(row))
    ):
        raise ValueError(_UNAVAILABLE)


def _proposal(session, code, actor, workspace, principal, *, version=None):
    _text(code, "code", 39, required=True)
    query = session.query(CoordinationProposal).filter(CoordinationProposal.code == code)
    if version is not None:
        _integer(version, "version", 1, 2**31 - 1)
        query = query.filter(CoordinationProposal.version == version)
    row = query.order_by(CoordinationProposal.version.desc()).first()
    _access(row, actor, workspace, principal)
    return row


def _revision(row, version, expected) -> None:
    _integer(version, "version", 1, 2**31 - 1)
    _integer(expected, "expected_revision", 0, 2**63 - 2)
    if row.version != version or row.revision != expected:
        raise ValueError("stale coordination version or revision; read the current proposal")


def _cas(session, row, **values) -> None:
    changed = (
        session.query(CoordinationProposal)
        .filter(
            CoordinationProposal.code == row.code,
            CoordinationProposal.version == row.version,
            CoordinationProposal.revision == row.revision,
        )
        .update(
            {**values, "revision": row.revision + 1, "updated_at": utc_now()},
            synchronize_session=False,
        )
    )
    if changed != 1:
        raise ValueError("stale coordination revision; read the current proposal")
    session.refresh(row)


def _open(row) -> None:
    if row.status not in _OPEN:
        raise ValueError("coordination is not open")
    if _aware(utc_now()) >= _aware(row.deadline_at):
        raise ValueError("coordination deadline exceeded")


def _peer(session, ident, workspace, principal) -> AgentSession:
    peer = session.get(AgentSession, ident)
    if (
        peer is None
        or peer.workspace_id != workspace.id
        or peer.created_by_operator_id != principal.operator_id
    ):
        raise ValueError(_UNAVAILABLE)
    require_live_session(session, ident, action="coordination", renew_lease=False)
    return peer


def _entries(session, row) -> list[CoordinationContribution]:
    return (
        session.query(CoordinationContribution)
        .filter(
            CoordinationContribution.proposal_code == row.code,
            CoordinationContribution.version == row.version,
        )
        .order_by(CoordinationContribution.created_at, CoordinationContribution.slot)
        .all()
    )


def _append(session, row, actor, kind, payload, *, round=0, key=None) -> None:
    slot = "final" if kind == "final" else f"{kind}:{round}:{actor.id}"
    session.add(
        CoordinationContribution(
            contribution_id=str(uuid4()),
            proposal_code=row.code,
            version=row.version,
            author_session_id=actor.id,
            kind=kind,
            round=round,
            slot=slot,
            idempotency_key=key,
            payload_json=payload,
            request_hash=_hash(_json({"kind": kind, "payload": json.loads(payload)})),
            created_at=utc_now(),
        )
    )


def _snapshot(session, row, viewer_session_id: str | None) -> dict:
    entries = _entries(session, row)
    peers = _participants(row)
    accepted = {e.author_session_id for e in entries if e.kind == "accept"}
    initial = {e.author_session_id for e in entries if e.kind == "initial"}
    discussion = {
        e.author_session_id for e in entries if e.kind == "discussion" and e.round == row.round
    }
    spec = json.loads(row.specification_json)
    exchange_mode = spec.get("exchange_mode", "rounds")
    if exchange_mode == "threaded":
        final_ready = row.status == "discussing" and (
            row.round == 0 or row.round > spec["discussion_rounds"]
        )
        discussion_authors = {e.author_session_id for e in entries if e.kind == "discussion"}
        remaining_discussion = (
            sorted(peers - discussion_authors)
            if row.status == "discussing" and not final_ready
            else []
        )
    else:
        final_ready = row.status == "discussing" and (
            row.round == 0 or row.round > spec["discussion_rounds"]
        )
        remaining_discussion = (
            sorted(peers - discussion) if row.status == "discussing" and not final_ready else []
        )
    visible = [
        e
        for e in entries
        if e.kind != "accept"
        and (
            row.initial_closed or (e.kind == "initial" and e.author_session_id == viewer_session_id)
        )
    ]
    contributions = [
        {
            "contribution_id": e.contribution_id,
            "author_session_id": e.author_session_id,
            "kind": e.kind,
            "round": e.round,
            "payload": json.loads(e.payload_json),
            "created_at": _aware(e.created_at).isoformat(),
        }
        for e in visible
    ]
    dissent = [
        {
            "contribution_id": e["contribution_id"],
            "author_session_id": e["author_session_id"],
            "round": e["round"],
            "dissent": e["payload"]["dissent"],
            "resolved": False,
        }
        for e in contributions
        if e["payload"].get("dissent", "").strip()
    ]
    expired = row.status in _OPEN and _aware(utc_now()) >= _aware(row.deadline_at)
    return {
        "code": row.code,
        "version": row.version,
        "revision": row.revision,
        "workspace_id": row.workspace_id,
        "creator_operator_id": row.creator_operator_id,
        "creator_kind": row.creator_kind,
        "requester_session_id": row.requester_session_id,
        "creator_session_id": row.requester_session_id,
        "result_owner_session_id": row.result_owner_session_id,
        "title": row.title,
        "specification": spec,
        "spec_hash": row.specification_hash,
        "status": row.status,
        "round": row.round,
        "initial_closed": row.initial_closed,
        "final_ready": final_ready and not expired,
        "expired_flag": expired,
        "incomplete_flag": row.status != "completed",
        "blinded": not row.initial_closed,
        "required_acceptance_session_ids": sorted(_members(row)),
        "accepted_session_ids": sorted(accepted),
        "remaining_acceptance_session_ids": sorted(_members(row) - accepted),
        "counts": {
            "participants": len(peers),
            "required_acceptances": len(_members(row)),
            "acceptances": len(accepted),
            "initial": len(initial),
            "discussion": len(discussion),
            "contributions": sum(e.kind != "accept" for e in entries),
            "visible_contributions": len(contributions),
        },
        "remaining_initial_session_ids": sorted(peers - initial),
        "remaining_discussion_session_ids": remaining_discussion,
        "contributions": contributions,
        "unresolved_dissent": dissent,
        "final": next((e["payload"] for e in contributions if e["kind"] == "final"), None),
        "deadline": _aware(row.deadline_at).isoformat(),
        "created_at": _aware(row.created_at).isoformat(),
        "updated_at": _aware(row.updated_at).isoformat(),
        "cancellation_reason": row.cancellation_reason,
    }


def _record_event(session, row, action: str, *, session_id: str | None, attribution: dict) -> None:
    kind = f"coordination_{action}"
    event = Event(
        workspace_id=row.workspace_id,
        session_id=session_id,
        kind=kind,
        message=f"{row.code}: {action}",
        metadata_json=_json(
            {
                "code": row.code,
                "version": row.version,
                "revision": row.revision,
                "round": row.round,
                "workspace_id": row.workspace_id,
                **attribution,
            }
        ),
    )
    session.add(event)
    session.flush()
    session.add(
        EventContext(
            event_id=event.id,
            category=classify_event_kind(kind),
            scope="workspace",
            scope_source="explicit",
            taxonomy_version=TAXONOMY_VERSION,
        )
    )


def _finish(session, row, actor, action: str) -> dict:
    require_live_session(session, actor.id, action="coordination", renew_lease=True)
    _record_event(session, row, action, session_id=actor.id, attribution={"actor_kind": "session"})
    result = _snapshot(session, row, actor.id)
    session.commit()
    return result


def propose_coordination(
    workspace_path: str,
    title: str,
    specification: dict,
    *,
    session_id: str,
    idempotency_key: str,
    code: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Create version 1 or replace an open latest version as its original requester.

    The requester acknowledges each new stored hash; only version 1 starts at zero.
    Replacement uses two consecutive revisions: old cancellation, then new proposal.
    Omitted deadline
    freezes now + one hour; explicit deadlines must be future and at most 30 days.
    Replacement never carries acknowledgements or contributions into the new version.
    """
    with _transaction(session_id, write=True) as (session, actor, workspace, principal):
        _workspace_path(session, workspace, workspace_path)
        title = _text(title, "title", 256, required=True)
        key = _text(idempotency_key, "idempotency_key", 128, required=True)
        spec = _specification(specification, actor.id)
        request = {"title": title, "specification": spec, "code": code}
        request_hash = _hash(_json({**request, "creator_kind": "session"}))
        old = None
        if code is not None:
            old = _proposal(session, code, actor, workspace, principal)
            if actor.id != old.requester_session_id:
                raise ValueError(_UNAVAILABLE)
            _revision(old, old.version, expected_revision)
        elif expected_revision is not None:
            raise ValueError("expected_revision requires a replacement code")
        replay = (
            session.query(CoordinationProposal)
            .filter(
                CoordinationProposal.workspace_id == workspace.id,
                CoordinationProposal.creator_operator_id == principal.operator_id,
                CoordinationProposal.idempotency_key == key,
            )
            .one_or_none()
        )
        if replay is not None:
            _access(replay, actor, workspace, principal)
            # Migration-155 creation keys predate creator_kind. Keep their retries
            # compatible, but never accept an operator key through a Session API.
            if replay.creator_kind != "session" or replay.request_hash not in {
                request_hash,
                _hash(_json(request)),
            }:
                raise ValueError("idempotency_key already used for a different request")
            current = _proposal(session, replay.code, actor, workspace, principal)
            return _snapshot(session, current, actor.id)
        if old is not None:
            _open(old)
        now = _aware(utc_now())
        deadline = _deadline(spec["deadline"]) if "deadline" in spec else now + timedelta(hours=1)
        if not now < deadline <= now + timedelta(days=MAX_DEADLINE_DAYS):
            raise ValueError("deadline must be future and within 30 days")
        spec["deadline"] = deadline.isoformat()
        participants = {p["session_id"] for p in spec["participants"]}
        owner = spec["result_owner_session_id"]
        if owner not in participants | {actor.id}:
            raise ValueError("result owner must be a participant or requester")
        for peer in spec["participants"]:
            stored = _peer(session, peer["session_id"], workspace, principal)
            peer["tool"] = _text(stored.tool, "tool", 64, required=True)
        _peer(session, owner, workspace, principal)
        encoded = _json(spec)
        _text(encoded, "specification", SPEC_LIMIT)
        if old is not None:
            _cas(
                session, old, status="cancelled", cancellation_reason="superseded by a new version"
            )
        row = CoordinationProposal(
            code=old.code if old is not None else f"PC-{uuid4()}",
            version=old.version + 1 if old is not None else 1,
            workspace_id=workspace.id,
            creator_operator_id=principal.operator_id,
            creator_kind="session",
            requester_session_id=actor.id,
            result_owner_session_id=owner,
            idempotency_key=key,
            request_hash=request_hash,
            title=title,
            specification_json=encoded,
            specification_hash=_hash(encoded),
            status="planned",
            revision=old.revision + 1 if old is not None else 0,
            round=0,
            initial_closed=False,
            deadline_at=deadline,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        _append(session, row, actor, "accept", _json({"spec_hash": row.specification_hash}))
        return _finish(session, row, actor, "replaced" if old is not None else "proposed")


def get_coordination(code: str, *, session_id: str, version: int | None = None) -> dict:
    with _transaction(session_id, write=False) as (session, actor, workspace, principal):
        return _snapshot(
            session,
            _proposal(session, code, actor, workspace, principal, version=version),
            actor.id,
        )


def list_coordinations(workspace_path: str, *, session_id: str, limit: int = 50) -> list:
    _integer(limit, "limit", 1, 200)
    with _transaction(session_id, write=False) as (session, actor, workspace, principal):
        _workspace_path(session, workspace, workspace_path)
        latest = (
            session.query(
                CoordinationProposal.code, func.max(CoordinationProposal.version).label("version")
            )
            .group_by(CoordinationProposal.code)
            .subquery()
        )
        rows = (
            session.query(CoordinationProposal)
            .join(
                latest,
                (
                    (CoordinationProposal.code == latest.c.code)
                    & (CoordinationProposal.version == latest.c.version)
                ),
            )
            .filter(
                CoordinationProposal.workspace_id == workspace.id,
                CoordinationProposal.creator_operator_id == principal.operator_id,
            )
            .order_by(CoordinationProposal.created_at.desc(), CoordinationProposal.code)
        )
        result = []
        for row in rows.yield_per(100):
            if actor.id in _members(row):
                result.append(_snapshot(session, row, actor.id))
                if len(result) == limit:
                    break
        return result


def accept_coordination(
    code: str, *, session_id: str, version: int, expected_revision: int, spec_hash: str
) -> dict:
    with _transaction(session_id, write=True) as (session, actor, workspace, principal):
        row = _proposal(session, code, actor, workspace, principal)
        _revision(row, version, expected_revision)
        _open(row)
        if spec_hash != row.specification_hash:
            raise ValueError("spec_hash does not match this version")
        accepted = {e.author_session_id for e in _entries(session, row) if e.kind == "accept"}
        if actor.id in accepted:
            return _snapshot(session, row, actor.id)
        if row.status != "planned":
            raise ValueError("coordination is not awaiting acceptance")
        _append(session, row, actor, "accept", _json({"spec_hash": spec_hash}))
        _cas(
            session, row, status="accepted" if accepted | {actor.id} == _members(row) else "planned"
        )
        return _finish(session, row, actor, "accepted")


def advance_coordination(
    code: str, *, session_id: str, version: int, expected_revision: int
) -> dict:
    with _transaction(session_id, write=True) as (session, actor, workspace, principal):
        row = _proposal(session, code, actor, workspace, principal)
        if actor.id != row.requester_session_id:
            raise ValueError(_UNAVAILABLE)
        _revision(row, version, expected_revision)
        _advance(session, row, workspace, principal)
        return _finish(session, row, actor, "advanced")


def submit_coordination(
    code: str,
    kind: str,
    payload: dict,
    *,
    session_id: str,
    version: int,
    expected_revision: int,
    idempotency_key: str,
) -> dict:
    with _transaction(session_id, write=True) as (session, actor, workspace, principal):
        row = _proposal(session, code, actor, workspace, principal)
        _revision(row, version, expected_revision)
        if type(kind) is not str or kind not in ("initial", "discussion", "final"):
            raise ValueError("kind must be initial, discussion, or final")
        key = _text(idempotency_key, "idempotency_key", 128, required=True)
        encoded = _payload(kind, payload)
        request_hash = _hash(_json({"kind": kind, "payload": json.loads(encoded)}))
        # Deadline is enforced even for a final lost-response replay; terminal
        # identical replays before deadline remain side-effect-free.
        if _aware(utc_now()) >= _aware(row.deadline_at):
            raise ValueError("coordination deadline exceeded")
        entries = _entries(session, row)
        replay = next(
            (e for e in entries if e.author_session_id == actor.id and e.idempotency_key == key),
            None,
        )
        if replay is not None:
            if replay.request_hash != request_hash:
                raise ValueError("idempotency_key already used for a different contribution")
            return _snapshot(session, row, actor.id)
        _open(row)
        spec = json.loads(row.specification_json)
        rounds = spec["discussion_rounds"]
        exchange_mode = spec.get("exchange_mode", "rounds")
        max_turns = spec.get("max_turns_per_peer", rounds or 3)
        if kind == "discussion":
            if exchange_mode == "threaded":
                peer_discussion = [
                    e for e in entries if e.kind == "discussion" and e.author_session_id == actor.id
                ]
                round = len(peer_discussion) + 1
                if round > max_turns:
                    raise ValueError("participant has reached maximum discussion turns")
            else:
                round = row.round
        else:
            round = 0
        if kind == "final":
            if actor.id != row.result_owner_session_id:
                raise ValueError(_UNAVAILABLE)
            if row.status != "discussing" or not (row.round == 0 or row.round > rounds):
                raise ValueError("coordination is not ready for a final synthesis")
        elif actor.id not in _participants(row):
            raise ValueError(_UNAVAILABLE)
        elif kind == "initial" and row.status != "collecting":
            raise ValueError("coordination is not collecting initial contributions")
        elif kind == "discussion":
            if row.status != "discussing":
                raise ValueError("coordination is not collecting discussion")
            if exchange_mode == "rounds" and not (1 <= round <= rounds):
                raise ValueError("coordination is not collecting this discussion round")
            if exchange_mode == "threaded" and row.round > rounds and rounds > 0:
                raise ValueError("discussion is closed for this coordination")
        if any(
            e.kind == kind and e.round == round and e.author_session_id == actor.id for e in entries
        ):
            raise ValueError("contribution slot already filled; contributions are append-only")
        _append(session, row, actor, kind, encoded, round=round, key=key)
        _cas(session, row, **({"status": "completed"} if kind == "final" else {}))
        return _finish(session, row, actor, "submitted")


def cancel_coordination(
    code: str, reason: str, *, session_id: str, version: int, expected_revision: int
) -> dict:
    with _transaction(session_id, write=True) as (session, actor, workspace, principal):
        row = _proposal(session, code, actor, workspace, principal)
        if actor.id != row.requester_session_id:
            raise ValueError(_UNAVAILABLE)
        _revision(row, version, expected_revision)
        reason = _text(reason, "reason", SPEC_LIMIT, required=True)
        if row.status == "cancelled" and row.cancellation_reason == reason:
            return _snapshot(session, row, actor.id)
        if row.status not in _OPEN:
            raise ValueError("coordination cannot be cancelled")
        _cas(session, row, status="cancelled", cancellation_reason=reason)
        return _finish(session, row, actor, "cancelled")


def _advance_blocked(row, entries) -> str | None:
    if row.status not in _OPEN:
        return "coordination_not_open"
    if _aware(utc_now()) >= _aware(row.deadline_at):
        return "deadline_exceeded"
    if row.status in ("planned", "accepted"):
        accepted = {e.author_session_id for e in entries if e.kind == "accept"}
        if accepted != _members(row):
            return "acceptances_required"
        return None if row.status == "accepted" else "cannot_advance_in_state"
    if row.status == "collecting":
        initial = {e.author_session_id for e in entries if e.kind == "initial"}
        return None if initial == _participants(row) else "initial_contributions_required"
    spec = json.loads(row.specification_json)
    rounds = spec["discussion_rounds"]
    exchange_mode = spec.get("exchange_mode", "rounds")
    if exchange_mode == "threaded":
        if row.status == "discussing":
            discussion_authors = {e.author_session_id for e in entries if e.kind == "discussion"}
            if discussion_authors != _participants(row):
                return "discussion_contributions_required"
            if row.round > rounds and rounds > 0:
                return "final_synthesis_required"
            return None
        return "cannot_advance_in_state"

    if 1 <= row.round <= rounds:
        discussion = {
            e.author_session_id for e in entries if e.kind == "discussion" and e.round == row.round
        }
        return None if discussion == _participants(row) else "discussion_contributions_required"
    return "final_synthesis_required"


_ADVANCE_ERRORS = {
    "coordination_not_open": "coordination is not open",
    "deadline_exceeded": "coordination deadline exceeded",
    "acceptances_required": "all acceptances are required before advancing",
    "initial_contributions_required": (
        "all initial contributions are required before closing collection"
    ),
    "discussion_contributions_required": (
        "all discussion contributions are required before advancing"
    ),
    "cannot_advance_in_state": "coordination cannot advance in this state",
    "final_synthesis_required": "coordination cannot advance before a final synthesis",
}


def _advance(session, row, workspace, principal, *, operator: bool = False) -> None:
    blocked = _advance_blocked(row, _entries(session, row))
    if blocked:
        raise ValueError(_ADVANCE_ERRORS[blocked])
    if operator or row.status == "accepted":
        # Human authority does not depend on the original requester remaining live.
        # Selected agents must still be live and owned under the writer reservation.
        required = _participants(row) | {row.result_owner_session_id} if operator else _members(row)
        for ident in sorted(required):
            _peer(session, ident, workspace, principal)
    if row.status == "accepted":
        _cas(session, row, status="collecting")
    elif row.status == "collecting":
        spec = json.loads(row.specification_json)
        rounds = spec["discussion_rounds"]
        exchange_mode = spec.get("exchange_mode", "rounds")
        _cas(
            session,
            row,
            status="discussing",
            initial_closed=True,
            round=1 if (rounds or exchange_mode == "threaded") else 0,
        )
    else:
        spec = json.loads(row.specification_json)
        rounds = spec["discussion_rounds"]
        exchange_mode = spec.get("exchange_mode", "rounds")
        if exchange_mode == "threaded":
            _cas(session, row, round=max(row.round, rounds) + 1)
        else:
            _cas(session, row, round=row.round + 1)


@contextmanager
def _operator_transaction(workspace_id: int, principal: Principal, *, write: bool):
    """Authenticate the supplied transport principal without a Session surrogate."""
    if (
        not isinstance(principal, Principal)
        or not principal.is_operator
        or principal.operator_id is None
    ):
        raise ValueError(_UNAVAILABLE)
    _integer(workspace_id, "workspace_id", 1, 2**63 - 1)
    capability = CAP_ORG_WRITE if write else CAP_ORG_READ
    require_workspace_capability(
        principal, capability, workspace_id, entity="coordination", ref=workspace_id
    )
    if write and not principal.is_human_channel:
        raise ValueError("operator coordination mutations require a human channel")
    init_db()
    with SessionLocal() as session:
        if write:
            # SQLite serializes this with Session/proposal/lifecycle writers. Do
            # not read proposal or peer state until the writer reservation exists.
            session.query(Workspace).filter(Workspace.id == workspace_id).update(
                {Workspace.id: Workspace.id}, synchronize_session=False
            )
        elif session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN")
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or session.get(Operator, principal.operator_id) is None:
            raise ValueError(_UNAVAILABLE)
        org_id = workspace.org_id
        if org_id is None:
            org_id = session.query(Org.id).filter(Org.slug == "default").scalar()
        if not principal.has_capability(capability, org_id) or not _visible(
            session, principal, workspace
        ):
            raise ValueError(_UNAVAILABLE)
        yield session, workspace, org_id


def _operator_snapshot(session, row, workspace, principal, org_id) -> dict:
    result = _snapshot(session, row, viewer_session_id=None)
    blocked = None
    if not principal.is_human_channel:
        blocked = "human_channel_required"
    elif not principal.has_capability(CAP_ORG_WRITE, org_id):
        blocked = "write_capability_required"
    elif row.version != (
        session.query(func.max(CoordinationProposal.version))
        .filter(CoordinationProposal.code == row.code)
        .scalar()
    ):
        blocked = "historical_version"
    advance_reason = blocked or _advance_blocked(row, _entries(session, row))
    if advance_reason is None:
        try:
            for ident in sorted(_participants(row) | {row.result_owner_session_id}):
                _peer(session, ident, workspace, principal)
        except ValueError:
            advance_reason = "participant_unavailable"
    # Expired open state may still be explicitly cancelled, as on the Session API.
    cancel_reason = blocked or ("coordination_not_open" if row.status not in _OPEN else None)
    result["permissions"] = {
        "can_advance": advance_reason is None,
        "advance_blocked_reason": advance_reason,
        "can_cancel": cancel_reason is None,
        "cancel_blocked_reason": cancel_reason,
    }
    return result


def _finish_operator(session, row, workspace, principal, org_id, action: str) -> dict:
    _record_event(
        session,
        row,
        action,
        session_id=None,
        attribution={
            "actor_kind": "operator",
            "operator_id": principal.operator_id,
            "channel": principal.channel,
        },
    )
    result = _operator_snapshot(session, row, workspace, principal, org_id)
    session.commit()
    return result


def list_operator_coordinations(
    workspace_id: int, *, principal: Principal, limit: int = 50
) -> list:
    """List the operator's latest versions as a blinded, non-member observer."""
    _integer(limit, "limit", 1, 200)
    with _operator_transaction(workspace_id, principal, write=False) as (
        session,
        workspace,
        org_id,
    ):
        latest = (
            session.query(
                CoordinationProposal.code, func.max(CoordinationProposal.version).label("version")
            )
            .group_by(CoordinationProposal.code)
            .subquery()
        )
        rows = (
            session.query(CoordinationProposal)
            .join(
                latest,
                (CoordinationProposal.code == latest.c.code)
                & (CoordinationProposal.version == latest.c.version),
            )
            .filter(
                CoordinationProposal.workspace_id == workspace.id,
                CoordinationProposal.creator_operator_id == principal.operator_id,
            )
            .order_by(CoordinationProposal.created_at.desc(), CoordinationProposal.code)
            .limit(limit)
            .all()
        )
        return [_operator_snapshot(session, row, workspace, principal, org_id) for row in rows]


def get_operator_coordination(
    workspace_id: int, code: str, *, principal: Principal, version: int | None = None
) -> dict:
    """Read latest or immutable historical scope with the same observer filter."""
    with _operator_transaction(workspace_id, principal, write=False) as (
        session,
        workspace,
        org_id,
    ):
        row = _proposal(session, code, None, workspace, principal, version=version)
        return _operator_snapshot(session, row, workspace, principal, org_id)


def propose_operator_coordination(
    workspace_id: int,
    title: str,
    specification: dict,
    *,
    principal: Principal,
    idempotency_key: str,
) -> dict:
    """Human agreement creates scope, never a synthetic peer acknowledgement.

    The explicit result owner may be another owned live Session outside the panel;
    it must also acknowledge the exact hash. Human replacement is not exposed.
    """
    with _operator_transaction(workspace_id, principal, write=True) as (session, workspace, org_id):
        title = _text(title, "title", 256, required=True)
        key = _text(idempotency_key, "idempotency_key", 128, required=True)
        spec = _specification(specification, None)
        request_hash = _hash(
            _json({"title": title, "specification": spec, "code": None, "creator_kind": "operator"})
        )
        replay = (
            session.query(CoordinationProposal)
            .filter(
                CoordinationProposal.workspace_id == workspace.id,
                CoordinationProposal.creator_operator_id == principal.operator_id,
                CoordinationProposal.idempotency_key == key,
            )
            .one_or_none()
        )
        if replay is not None:
            if replay.creator_kind != "operator" or replay.request_hash != request_hash:
                raise ValueError("idempotency_key already used for a different request")
            current = _proposal(session, replay.code, None, workspace, principal)
            return _operator_snapshot(session, current, workspace, principal, org_id)
        now = _aware(utc_now())
        deadline = _deadline(spec["deadline"]) if "deadline" in spec else now + timedelta(hours=1)
        if not now < deadline <= now + timedelta(days=MAX_DEADLINE_DAYS):
            raise ValueError("deadline must be future and within 30 days")
        spec["deadline"] = deadline.isoformat()
        for participant in spec["participants"]:
            stored = _peer(session, participant["session_id"], workspace, principal)
            participant["tool"] = _text(stored.tool, "tool", 64, required=True)
        owner = spec["result_owner_session_id"]
        _peer(session, owner, workspace, principal)
        encoded = _json(spec)
        _text(encoded, "specification", SPEC_LIMIT)
        row = CoordinationProposal(
            code=f"PC-{uuid4()}",
            version=1,
            workspace_id=workspace.id,
            creator_operator_id=principal.operator_id,
            creator_kind="operator",
            requester_session_id=None,
            result_owner_session_id=owner,
            idempotency_key=key,
            request_hash=request_hash,
            title=title,
            specification_json=encoded,
            specification_hash=_hash(encoded),
            status="planned",
            revision=0,
            round=0,
            initial_closed=False,
            deadline_at=deadline,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return _finish_operator(session, row, workspace, principal, org_id, "proposed")


def advance_operator_coordination(
    workspace_id: int, code: str, *, principal: Principal, version: int, expected_revision: int
) -> dict:
    """Explicitly advance owned scope, including Session-created latest versions."""
    with _operator_transaction(workspace_id, principal, write=True) as (session, workspace, org_id):
        row = _proposal(session, code, None, workspace, principal)
        _revision(row, version, expected_revision)
        _advance(session, row, workspace, principal, operator=True)
        return _finish_operator(session, row, workspace, principal, org_id, "advanced")


def cancel_operator_coordination(
    workspace_id: int,
    code: str,
    reason: str,
    *,
    principal: Principal,
    version: int,
    expected_revision: int,
) -> dict:
    """Cancel latest owned scope without changing agents, assignments, or blinding."""
    with _operator_transaction(workspace_id, principal, write=True) as (session, workspace, org_id):
        row = _proposal(session, code, None, workspace, principal)
        _revision(row, version, expected_revision)
        reason = _text(reason, "reason", SPEC_LIMIT, required=True)
        if row.status == "cancelled" and row.cancellation_reason == reason:
            return _operator_snapshot(session, row, workspace, principal, org_id)
        if row.status not in _OPEN:
            raise ValueError("coordination cannot be cancelled")
        _cas(session, row, status="cancelled", cancellation_reason=reason)
        return _finish_operator(session, row, workspace, principal, org_id, "cancelled")
