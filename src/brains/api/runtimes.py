"""Hub runtime router for daemon registration, assignments, and events.

Authentication resolves one principal (:mod:`brains.authz.deps`); authorization
is per route, because this router is the one surface a **Runtime-narrow**
credential is allowed to reach.

* A *Runtime* credential (minted by enrollment redemption) may act only on the
  machine it was minted for, inside the Org it was bound to, and only for the
  Runtime operations named in ``brains.authz.principal.RUNTIME_OPERATIONS``:
  register, heartbeat, status, claim, execute. Every other route in the
  product refuses it.
* An *operator* credential needs a role in the Runtime's Org: ``member`` to
  read, ``admin`` to change Runtime lifecycle (patch/deregister) or to mint an
  enrollment token.
* A Runtime that belongs to another Org is answered ``404`` rather than
  ``403``, so Runtime IDs cannot be enumerated across Orgs.

Enrollment redemption keeps its one-time token as the credential and stays
unauthenticated. The router is otherwise a thin HTTP shell over the control
layer:

* registration / heartbeat / liveness  → :mod:`brains.control.runtimes`
* assignment poll / claim / ack / sessions / events → :mod:`brains.control.assignments`

It invents no persistence. The daemon spawns work through ``exec.runner``;
the current cooperative gate and its limitations are documented in
``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from brains.authz import policy
from brains.authz.deps import require_console_principal
from brains.authz.principal import CAP_ORG_ADMIN, CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.control import assignments as assignments_ctl
from brains.control import enrolment as enrolment_ctl
from brains.control import runtimes as runtimes_ctl

# Authoritative timing knobs the hub hands back on register (§2.5). The daemon
# treats its own config as defaults and these as the override.
HEARTBEAT_INTERVAL_S = 15
TTL_S = 3 * HEARTBEAT_INTERVAL_S  # = 45s (survives one missed beat, §3.4)
ASSIGNMENTS_POLL_S = 3
DETECT_INTERVAL_S = 300

INTERVALS = {
    "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
    "ttl_s": TTL_S,
    "assignments_poll_s": ASSIGNMENTS_POLL_S,
    "detect_interval_s": DETECT_INTERVAL_S,
}

router = APIRouter(prefix="/v1/runtimes")

# Public (UNAUTHENTICATED) sibling router for token redemption. The enrolment
# token IS the credential, so the redeem route must not sit behind an operator
# gate. It carries ONLY the redeem route — nothing else.
enrol_public = APIRouter(prefix="/v1/runtimes")


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class ToolSpec(BaseModel):
    tool: str
    display_name: str | None = None
    capabilities: dict | None = None


class RegisterBody(BaseModel):
    machine_id: str
    machine_label: str | None = None
    os: str | None = None
    daemon_version: str | None = None
    working_root: str | None = None
    org_id: int | None = None
    tools: list[ToolSpec] = []


class HeartbeatBody(BaseModel):
    status: str | None = None
    health: str | None = None
    load: dict | None = None
    capabilities: dict | None = None


class PatchRuntimeBody(BaseModel):
    status: str | None = None
    health: str | None = None


class BatchHeartbeatItem(BaseModel):
    id: int | None = None
    tool: str | None = None
    status: str | None = None
    health: str | None = None
    load: dict | None = None
    capabilities: dict | None = None


class BatchHeartbeatBody(BaseModel):
    machine_id: str
    runtimes: list[BatchHeartbeatItem] = []


class ClaimAckBody(BaseModel):
    state: str
    session_id: str | None = None
    returncode: int | None = None


class OpenSessionBody(BaseModel):
    persona_id: int | None = None
    issue_id: int | None = None
    workspace_path: str | None = None
    tool: str | None = None
    session_id: str | None = None
    pid: int | None = None


class SessionEventBody(BaseModel):
    seq: int
    stream: str = "stdout"
    chunk: str = ""
    ts: str | None = None
    exec_id: str | None = None


class SessionCommandAckBody(BaseModel):
    """The outcome a Runtime observed for one claimed Session command."""

    result: str
    ok: bool = True
    error: str | None = None


class SessionCommandReleaseBody(BaseModel):
    """Why a Runtime is handing a claimed command back to the queue."""

    reason: str | None = None


class SessionReconcileBody(BaseModel):
    """The Sessions a Runtime can still prove it owns a live process for."""

    owned_session_ids: list[str] = []
    reason: str | None = None


class EnrolBody(BaseModel):
    label: str | None = None
    org_id: int | None = None
    ttl_seconds: int = 900


class EnrolCliSpec(BaseModel):
    tool: str
    version: str | None = None


class EnrolRedeemBody(BaseModel):
    token: str
    machine_id: str
    clis: list[EnrolCliSpec] = []
    org_id: int | None = None


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


# --------------------------------------------------------------------------- #
# Authorization helpers
# --------------------------------------------------------------------------- #


def _declared_machine_org_id(machine_id: str) -> int | None:
    """The Org a machine is actually *claimed* by, without a fallback.

    The claim is whatever its Runtimes declare, or - when an enrolment
    registered no tool - the Org of its live Runtime credential. A pre-Org
    registration (``org_id IS NULL`` and no credential) claims nothing, so it
    must not be read as "belongs to the default Org" - that would refuse a
    Runtime credential minted by an Org-less enrollment on its own machine.
    """
    return runtimes_ctl.machine_org_claim(machine_id)


def _machine_org_id(machine_id: str) -> int | None:
    """The Org a machine's already-registered Runtimes belong to."""
    rows = runtimes_ctl.list_runtimes(machine_id=machine_id)
    declared = _declared_machine_org_id(machine_id)
    if declared is not None:
        return declared
    return policy.default_org_id() if rows else None


def _authorize_machine(
    principal: Principal,
    machine_id: str,
    *,
    operation: str,
    capability: str,
    requested_org_id: int | None = None,
) -> int | None:
    """Authorize an action addressed by machine. Returns the effective Org id.

    A machine that has already *declared* an Org belongs to it, and that
    binding wins over anything the caller asks for. Without this, an ``admin``
    of Org B could re-register another Org's machine into its own Org - a
    single ``POST /v1/runtimes/register`` would silently move every Runtime on
    that box, along with the work assigned to it.

    The refusal is non-disclosing: a caller that names an Org the machine is
    not in gets the same ``404`` as for a machine that does not exist, so the
    route cannot be used to probe which Org owns which machine id.
    """
    claimed_org_id = _declared_machine_org_id(machine_id)
    if principal.is_runtime:
        policy.authorize_runtime_operation(principal, operation, machine_id=machine_id)
        if requested_org_id is not None and requested_org_id != principal.runtime_org_id:
            raise policy.not_found("runtime", machine_id)
        if claimed_org_id is not None and claimed_org_id != principal.runtime_org_id:
            raise policy.not_found("runtime", machine_id)
        return principal.runtime_org_id
    if claimed_org_id is not None:
        if requested_org_id is not None and requested_org_id != claimed_org_id:
            # Authorize against the Org that actually owns the machine first,
            # so a principal with no standing there cannot even learn that the
            # requested Org is the wrong one.
            policy.require_capability(
                principal, capability, claimed_org_id, entity="runtime", ref=machine_id
            )
            raise policy.not_found("runtime", machine_id)
        org_id: int | None = claimed_org_id
    else:
        org_id = requested_org_id
        if org_id is None:
            org_id = _machine_org_id(machine_id)
        if org_id is None:
            org_id = policy.default_org_id()
    policy.require_capability(principal, capability, org_id, entity="runtime", ref=machine_id)
    return org_id


def _authorize_runtime(
    principal: Principal,
    runtime_ref: str | int,
    *,
    operation: str,
    capability: str,
) -> dict:
    """Resolve a Runtime and authorize the principal against its Org.

    A Runtime credential is checked on **both** of its bindings, not only the
    machine: the row's Org must be the Org the credential was minted for. The
    machine check alone would trust whatever Org a credential claims for a
    machine, so any path that could mint a credential for somebody else's
    machine id would immediately confer access to that machine's Runtimes.
    Defence in depth is the point - enrolment refuses to mint such a credential
    in the first place, and this refuses to honour one that exists.

    A Runtime row with no Org is a pre-Org legacy registration. It is *not*
    read as belonging to the ``default`` Org: nobody claimed it, so treating it
    as claimed would hand a legacy machine to whichever Org happens to be the
    default. It stays classified as unclaimed and only the install
    administrator can act on it - re-registering the machine is what gives it
    an owner.
    """
    row = runtimes_ctl.get_runtime(runtime_ref)
    if row is None:
        raise policy.not_found("runtime", runtime_ref)
    if principal.is_runtime:
        policy.authorize_runtime_operation(
            principal,
            operation,
            machine_id=row.get("machine_id"),
            org_id=row.get("org_id"),
        )
        return row
    org_id = row.get("org_id")
    if org_id is None:
        if not principal.is_bootstrap_admin:
            raise policy.not_found("runtime", runtime_ref)
        return row
    policy.require_capability(principal, capability, org_id, entity="runtime", ref=runtime_ref)
    return row


def _visible_runtimes(principal: Principal, rows: list[dict]) -> list[dict]:
    """Filter a Runtime listing down to what the principal may see.

    A Runtime credential sees its own machine, and only the rows on it that its
    own Org owns - the same two-sided check every per-ID surface applies, so a
    listing cannot show what a detail read would refuse.

    An Org-less row is visible to the install administrator only, for the same
    reason :func:`_authorize_runtime` refuses it: it is an unclaimed legacy
    registration, and showing it to the ``default`` Org would be an attribution
    nothing in the data supports.
    """
    if principal.is_runtime:
        return [
            r
            for r in rows
            if r.get("machine_id") == principal.runtime_machine_id
            and r.get("org_id") in (None, principal.runtime_org_id)
        ]
    org_ids = principal.visible_org_ids()
    if org_ids is None:
        return rows
    return [r for r in rows if r.get("org_id") is not None and r["org_id"] in org_ids]


def _runtime_scope_org(principal: Principal, runtime: dict) -> int | None:
    """The Org a Runtime call is bound to, or ``None`` when it claims none.

    A Runtime credential is always Org-bound by its enrollment token, so that
    binding wins. A Runtime *row* with ``org_id IS NULL`` is a pre-Org legacy
    registration: it makes no Org claim, so it neither grants nor blocks, and
    the operator path has already passed a capability check to reach it.
    """
    if principal.is_runtime:
        return principal.runtime_org_id
    return runtime.get("org_id")


def _assert_issue_in_runtime_org(principal: Principal, runtime: dict, issue_id: int | None) -> None:
    """An Issue named by a Runtime call must live in that Runtime's Org."""
    scope = _runtime_scope_org(principal, runtime)
    if issue_id is None or scope is None:
        return
    if policy.issue_org_id(issue_id) != scope:
        raise policy.not_found("issue", issue_id)


def _assert_persona_in_runtime_org(
    principal: Principal, runtime: dict, persona_id: int | None
) -> None:
    """A Persona named by a Runtime call must live in that Runtime's Org."""
    scope = _runtime_scope_org(principal, runtime)
    if persona_id is None or scope is None:
        return
    if policy.persona_org_id(persona_id) != scope:
        raise policy.not_found("persona", persona_id)


def _assert_session_belongs_to_runtime(
    principal: Principal, runtime: dict, session_id: str | None
) -> None:
    """A Session named by a Runtime call must already belong to that Runtime.

    ``open_session`` patches an existing row in place when the id is known, and
    the event ingest writes into whatever Session it is given, so without this
    a machine could adopt another Org's Session and inject transcript content
    into the console its operators are watching.

    The binding is the strong check: a Session already bound to a different
    Runtime is refused outright. The Org comparison is a second line for a
    still-unbound Session, and it deliberately ignores the ``default`` Org,
    which is the legacy bucket a Workspace created on the fly lands in and is
    therefore not a distinctive claim.
    """
    if not session_id:
        return
    from brains.control import sessions as sessions_ctl

    row = sessions_ctl.get_agent_session(session_id)
    if row is None:
        return
    if row.get("runtime_id") not in (None, runtime.get("id")):
        raise policy.not_found("session", session_id)
    scope = _runtime_scope_org(principal, runtime)
    if scope is None:
        return
    session_org = policy.workspace_declared_org_id(row.get("workspace_id"))
    if session_org is None or session_org == policy.default_org_id():
        return
    if session_org != scope:
        raise policy.not_found("session", session_id)


def _assert_assignment_in_runtime_org(
    principal: Principal, runtime: dict, assignment_id: str
) -> None:
    """An assignment id names an Issue; it must be in the Runtime's Org.

    Assignment ids are derived from the Issue id, so the id space is trivially
    enumerable: without this, one machine credential could transition every
    other Org's Issues.
    """
    scope = _runtime_scope_org(principal, runtime)
    if scope is None:
        return
    from brains.control.assignments import issue_id_from_assignment

    try:
        issue_id = issue_id_from_assignment(assignment_id)
    except ValueError as exc:
        raise policy.not_found("assignment", assignment_id) from exc
    if policy.issue_org_id(issue_id) != scope:
        raise policy.not_found("assignment", assignment_id)


# --------------------------------------------------------------------------- #
# Registration + liveness
# --------------------------------------------------------------------------- #


@router.post("/register")
def register(
    body: RegisterBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Register/upsert all detected tools on a machine (§2). One ``runtimes`` row
    per tool; ``registered_tools`` is upserted first so the hard FK holds."""
    org_id = _authorize_machine(
        principal,
        body.machine_id,
        operation="runtime.register",
        capability=CAP_ORG_ADMIN,
        requested_org_id=body.org_id,
    )
    out: list[dict] = []
    for spec in body.tools:
        caps = json.dumps(spec.capabilities) if spec.capabilities is not None else None
        try:
            rt = runtimes_ctl.register_runtime(
                body.machine_id,
                spec.tool,
                org_id=org_id,
                machine_label=body.machine_label,
                display_name=spec.display_name,
                daemon_version=body.daemon_version,
                os=body.os,
                working_root=body.working_root,
                capabilities=caps,
                status="online",
                health="healthy",
            )
        except runtimes_ctl.RuntimeOrgConflictError as exc:
            # The machine was claimed by another Org between the authorization
            # read and this write. Answer exactly as for an unknown machine.
            raise policy.not_found("runtime", body.machine_id) from exc
        except ValueError as exc:
            raise _bad_request(exc) from exc
        out.append(
            {
                "tool": rt["tool"],
                "id": rt["id"],
                "slug": rt["slug"],
                "status": rt["status"],
                **INTERVALS,
            }
        )
    return {"runtimes": out, **INTERVALS}


@router.post("/heartbeat")
def heartbeat_batch(
    body: BatchHeartbeatBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Batched heartbeat for a machine (§3.1, preferred). One request amortises
    N tools on a box into a single round-trip."""
    _authorize_machine(
        principal,
        body.machine_id,
        operation="runtime.heartbeat",
        capability=CAP_ORG_WRITE,
    )
    out: list[dict] = []
    for item in body.runtimes:
        ref: str | int | None = item.id
        if ref is None and item.tool is not None:
            existing = runtimes_ctl.list_runtimes(machine_id=body.machine_id, tool=item.tool)
            if not existing:
                continue
            ref = existing[0]["id"]
        if ref is None:
            continue
        existing_row = runtimes_ctl.get_runtime(ref)
        if existing_row is None or existing_row.get("machine_id") != body.machine_id:
            # A heartbeat may only name a Runtime on the machine it claims to
            # be, whichever credential presented it.
            continue
        if principal.is_runtime and existing_row.get("org_id") not in (
            None,
            principal.runtime_org_id,
        ):
            # Both bindings, not just the machine: a Runtime credential never
            # writes to a Runtime row another Org owns.
            continue
        try:
            rt = runtimes_ctl.heartbeat(ref, status=item.status, health=item.health)
        except ValueError:
            continue
        _publish_runtime("runtime.heartbeat", rt)
        out.append(rt)
    return {"runtimes": out, **INTERVALS}


def _publish_runtime(event_type: str, rt: dict) -> None:
    """Best-effort runtime event fan-out (WS3 §3.3).

    ``runtime.heartbeat`` is deliberately *notification only*. It fires every
    ``HEARTBEAT_INTERVAL_S`` for every Runtime on every box, and the state a
    user would notice losing is the ``runtime.status`` transition, not the
    liveness tick. Recording every tick would spend three durable writes per
    heartbeat and churn the shared replay window, so a console that was away
    for an hour would be told to resynchronise because of liveness traffic it
    never wanted. Persist-before-publish and Org resolution for the events
    that do matter live in :mod:`brains.api.realtime_publish`; a realtime
    failure never breaks the write that triggered it.
    """
    import contextlib

    if event_type == "runtime.heartbeat":
        from brains.authz import policy
        from brains.events import topics as topic_grammar
        from brains.events.bus import publish

        with contextlib.suppress(Exception):
            org_id = policy.runtime_org_id(rt.get("id"))
            publish(
                topic_grammar.org_topic(org_id, "runtimes"),
                event_type,
                entity="runtime",
                id=rt.get("id"),
                payload=rt,
                org_id=org_id,
            )
        return

    from brains.api.realtime_publish import publish_runtime

    with contextlib.suppress(Exception):
        publish_runtime(rt.get("org_id"), event_type, rt)


@router.post("/{runtime_id}/heartbeat")
def heartbeat_single(
    runtime_id: str,
    body: HeartbeatBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Per-runtime heartbeat (§3). Updates liveness; ``offline`` is never
    self-reported (GC asserts it)."""
    _authorize_runtime(
        principal, runtime_id, operation="runtime.heartbeat", capability=CAP_ORG_WRITE
    )
    try:
        rt = runtimes_ctl.heartbeat(runtime_id, status=body.status, health=body.health)
    except ValueError as exc:
        raise _not_found(exc) from exc
    _publish_runtime("runtime.heartbeat", rt)
    return {**rt, **INTERVALS}


@router.get("")
def list_runtimes(
    status: str | None = None,
    health: str | None = None,
    org_id: int | None = None,
    tool: str | None = None,
    machine_id: str | None = None,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Operator/UI + daemon self-check read surface (§8.1 #9)."""
    if org_id is not None and not principal.can_see_org(org_id):
        raise policy.not_found("org", org_id)
    rows = runtimes_ctl.list_runtimes(
        org_id=org_id, machine_id=machine_id, tool=tool, status=status
    )
    if health is not None:
        rows = [r for r in rows if r.get("health") == health]
    return {"runtimes": _visible_runtimes(principal, rows)}


@router.get("/{runtime_id}")
def get_runtime(
    runtime_id: str,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    return _authorize_runtime(
        principal, runtime_id, operation="runtime.status", capability=CAP_ORG_READ
    )


@router.patch("/{runtime_id}")
def patch_runtime(
    runtime_id: str,
    body: PatchRuntimeBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Operator-driven status/health update (e.g. drain → ``draining``). Emits a
    best-effort ``runtime.status`` event so live grids update without a refresh."""
    policy.require_operator(principal, operation="runtime lifecycle")
    _authorize_runtime(principal, runtime_id, operation="runtime.status", capability=CAP_ORG_ADMIN)
    try:
        rt = runtimes_ctl.set_status(runtime_id, status=body.status, health=body.health)
    except ValueError as exc:
        msg = str(exc)
        if "unknown runtime" in msg:
            raise _not_found(exc) from exc
        raise _bad_request(exc) from exc
    _publish_runtime("runtime.status", rt)
    return rt


@router.delete("/{runtime_id}")
def deregister(
    runtime_id: str,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Graceful deregister → ``offline`` (never hard-delete; FK history, §6)."""
    policy.require_operator(principal, operation="runtime deregistration")
    _authorize_runtime(principal, runtime_id, operation="runtime.status", capability=CAP_ORG_ADMIN)
    try:
        rt = runtimes_ctl.mark_offline(runtime_id)
    except ValueError as exc:
        raise _not_found(exc) from exc
    return {"deregistered": True, "runtime": rt}


# --------------------------------------------------------------------------- #
# Enrolment (Connect a machine, F1) — mint (authed) + redeem (public)
# --------------------------------------------------------------------------- #


@router.post("/enrol")
def enrol(
    body: EnrolBody,
    request: Request,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Mint a single-use connect token and the one-line connect command (F1.1).

    Minting a connect token grants a machine a credential in an Org, so it is
    an ``admin`` capability in the Org the token names. The hub base URL is
    derived from the request (no hardcoding, no ``<url>`` placeholder). The
    minted raw token is returned ONCE and embedded in the command; only its
    hash is persisted server-side.
    """
    policy.require_operator(principal, operation="enrollment minting")
    org_id = body.org_id if body.org_id is not None else policy.default_org_id()
    policy.require_capability(principal, CAP_ORG_ADMIN, org_id, entity="org", ref=body.org_id)
    minted = enrolment_ctl.mint_token(
        label=body.label,
        org_id=org_id,
        ttl_seconds=body.ttl_seconds,
        operator={"id": principal.operator_id},
    )
    base_url = str(request.base_url).rstrip("/")
    command = f"brains-ai daemon start --hub {base_url} --enrol {minted['token']}"
    return {
        "token": minted["token"],
        "expires_at": minted["expires_at"],
        "command": command,
        "id": minted["id"],
        "label": minted["label"],
    }


@enrol_public.post("/enrol/redeem")
def enrol_redeem(body: EnrolRedeemBody) -> dict:
    """Redeem a connect token WITHOUT an operator key (F1.2).

    The token is the credential; this route is intentionally unauthenticated.
    Registers one runtime per CLI, version-stamped, and returns the machine's
    Runtime-narrow credential exactly once.
    """
    try:
        return enrolment_ctl.redeem_token(
            body.token,
            machine_id=body.machine_id,
            clis=[spec.model_dump() for spec in body.clis],
            org_id=body.org_id,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


# --------------------------------------------------------------------------- #
# Assignments (poll → claim → ack) + sessions + events
# --------------------------------------------------------------------------- #


@router.get("/{runtime_id}/assignments")
def get_assignments(
    runtime_id: str,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    _authorize_runtime(principal, runtime_id, operation="runtime.claim", capability=CAP_ORG_READ)
    try:
        items = assignments_ctl.list_assignments_for_runtime(runtime_id)
    except ValueError as exc:
        raise _not_found(exc) from exc
    return {"assignments": items}


@router.post("/{runtime_id}/assignments/{aid}/claim")
def claim_assignment(
    runtime_id: str,
    aid: str,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.claim", capability=CAP_ORG_WRITE
    )
    _assert_assignment_in_runtime_org(principal, runtime, aid)
    try:
        return assignments_ctl.claim_assignment(runtime_id, aid)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{runtime_id}/assignments/{aid}/ack")
def ack_assignment(
    runtime_id: str,
    aid: str,
    body: ClaimAckBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.claim", capability=CAP_ORG_WRITE
    )
    _assert_assignment_in_runtime_org(principal, runtime, aid)
    _assert_session_belongs_to_runtime(principal, runtime, body.session_id)
    try:
        return assignments_ctl.ack_assignment(
            runtime_id,
            aid,
            body.state,
            session_id=body.session_id,
            returncode=body.returncode,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{runtime_id}/sessions")
def open_session(
    runtime_id: str,
    body: OpenSessionBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Open an ``agent_sessions`` row stamped {runtime_id, persona_id, issue_id}
    (§5.4). The hub owns the write so a remote daemon needs no DB access."""
    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.execute", capability=CAP_ORG_WRITE
    )
    _assert_session_belongs_to_runtime(principal, runtime, body.session_id)
    _assert_persona_in_runtime_org(principal, runtime, body.persona_id)
    _assert_issue_in_runtime_org(principal, runtime, body.issue_id)
    try:
        return assignments_ctl.open_session(
            runtime_id,
            persona_id=body.persona_id,
            issue_id=body.issue_id,
            workspace_path=body.workspace_path,
            tool=body.tool,
            session_id=body.session_id,
            pid=body.pid,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/{runtime_id}/sessions/{sid}/events")
def session_event(
    runtime_id: str,
    sid: str,
    body: SessionEventBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Durable stdout/lifecycle event ingest (§5.3 fallback)."""
    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.execute", capability=CAP_ORG_WRITE
    )
    _assert_session_belongs_to_runtime(principal, runtime, sid)
    try:
        return assignments_ctl.record_session_event(
            runtime_id,
            sid,
            seq=body.seq,
            stream=body.stream,
            chunk=body.chunk,
            ts=body.ts,
            exec_id=body.exec_id,
        )
    except ValueError as exc:
        raise _not_found(exc) from exc


# --------------------------------------------------------------------------- #
# Session commands (poll → claim → acknowledge) + reconciliation, BL-P0-05
# --------------------------------------------------------------------------- #


def _assert_command_belongs_to_runtime(principal: Principal, runtime: dict, command: dict) -> None:
    """A Session command may only be consumed by the Runtime it belongs to.

    Command ids are opaque, but they are still handed out over an authenticated
    surface, so the binding is checked rather than assumed: the command's
    Session must be bound to *this* Runtime. Sharing a machine is not
    ownership - a second worker, a Session the operator started from the CLI,
    and the hub's own console Session all live on the same box and hold their
    own process handles - so a command whose Session names another Runtime, or
    no Runtime at all, is not this caller's to take. Without it, one machine's
    credential could claim and settle another consumer's operator commands,
    and a settled command is an outcome an operator is shown.

    The command's machine stamp is deliberately not compared against the
    Runtime's. It records where the row was *created* - the hub's own machine,
    for a Session spawned from the console - so requiring it to match would
    refuse the remote Runtime that holds the agent process the command exists
    to reach.
    """
    from brains.control import session_commands as commands_ctl

    if not commands_ctl.owned_by(command, runtime_id=runtime.get("id")):
        raise policy.not_found("session command", command.get("command_id"))
    _assert_session_belongs_to_runtime(principal, runtime, command.get("session_id"))


def _consumer_id(runtime: dict) -> str:
    return f"runtime:{runtime.get('id')}:{runtime.get('machine_id')}"


@router.get("/{runtime_id}/session-commands")
def get_session_commands(
    runtime_id: str,
    limit: int = 25,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Claimable operator commands for the Sessions bound to this Runtime.

    Listed by binding alone. A Runtime is the owner of its Sessions' commands
    wherever the rows were stamped, so a Runtime with no machine recorded, and
    one whose Sessions were opened by the hub, still see their own work.
    """
    from brains.control import session_commands as commands_ctl

    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.claim", capability=CAP_ORG_READ
    )
    items = commands_ctl.list_open_for_consumer(
        runtime_id=runtime.get("id"), limit=max(1, min(limit, 100))
    )
    return {"commands": items}


@router.post("/{runtime_id}/session-commands/{command_id}/claim")
def claim_session_command(
    runtime_id: str,
    command_id: str,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Claim one command with a lease. Exactly one caller wins."""
    from brains.control import session_commands as commands_ctl

    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.claim", capability=CAP_ORG_WRITE
    )
    command = commands_ctl.get(command_id)
    if command is None:
        raise policy.not_found("session command", command_id)
    _assert_command_belongs_to_runtime(principal, runtime, command)
    consumer = _consumer_id(runtime)
    try:
        claimed = commands_ctl.claim(
            command_id,
            consumer=consumer,
            runtime_id=runtime.get("id"),
        )
    except commands_ctl.NotOwnedError as exc:  # pragma: no cover - re-bound mid-claim
        raise policy.not_found("session command", command_id) from exc
    except commands_ctl.SessionCommandError as exc:
        raise _bad_request(exc) from exc
    if claimed is None:
        return {"claimed": False, "command_id": command_id, "reason": "already_claimed"}
    return {"claimed": True, "command": claimed}


@router.post("/{runtime_id}/session-commands/{command_id}/release")
def release_session_command(
    runtime_id: str,
    command_id: str,
    body: SessionCommandReleaseBody | None = None,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Hand a claimed command back to the queue without settling it.

    This is what a Runtime does with a command it holds but does not own - one
    whose Session was re-bound while it was in flight - and with whatever it
    still holds when it shuts down. The alternative, settling it ``failed``,
    would report an outcome for a delivery that was never attempted and would
    consume the command its real owner is about to claim.

    Only the current holder can release, which is enforced in the store, so
    this cannot reopen an attempt that has already been reassigned: a caller
    that is not the holder is answered ``not_held`` and changes nothing. The
    machine stamp on the row is not consulted, because it is not an ownership
    fact - a spawn Session is stamped with the hub's machine rather than the
    Runtime's, and comparing it would refuse a Runtime the right to hand back
    a command it is actually holding.
    """
    from brains.control import session_commands as commands_ctl

    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.execute", capability=CAP_ORG_WRITE
    )
    command = commands_ctl.get(command_id)
    if command is None:
        raise policy.not_found("session command", command_id)
    payload = body or SessionCommandReleaseBody()
    released = commands_ctl.release(
        command_id, consumer=_consumer_id(runtime), reason=payload.reason
    )
    if released is None:
        return {"released": False, "command_id": command_id, "reason": "not_held"}
    return {"released": True, "command": released}


@router.post("/{runtime_id}/session-commands/{command_id}/ack")
def ack_session_command(
    runtime_id: str,
    command_id: str,
    body: SessionCommandAckBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Settle a claimed command with the outcome the Runtime observed.

    Only the current lease holder may settle, so a Runtime whose lease expired
    and was re-claimed cannot overwrite the outcome of the attempt that
    replaced it. The holder is *also* allowed to settle a command whose
    Session was re-bound while it was in flight: it delivered that attempt and
    observed its outcome, and discarding a true report because the binding
    moved would lose the one fact nobody else has. A caller that is not the
    holder must own the command outright.
    """
    from brains.control import session_commands as commands_ctl

    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.execute", capability=CAP_ORG_WRITE
    )
    command = commands_ctl.get(command_id)
    if command is None:
        raise policy.not_found("session command", command_id)
    consumer = _consumer_id(runtime)
    if command.get("claimed_by") != consumer:
        _assert_command_belongs_to_runtime(principal, runtime, command)
    try:
        return commands_ctl.acknowledge(
            command_id,
            consumer=consumer,
            result=body.result,
            error=body.error,
            ok=body.ok,
        )
    except commands_ctl.SessionCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{runtime_id}/sessions/reconcile")
def reconcile_sessions(
    runtime_id: str,
    body: SessionReconcileBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Reconcile what the hub believes is running here with what is.

    A Runtime that restarted holds no process handles, so every Session the
    hub still shows as running for this Runtime and it cannot prove it owns is
    brought to a terminal state with a truthful summary, and its queued
    commands are cancelled rather than left pending forever. Sessions younger
    than the reconciliation grace window are left alone, because a daemon
    opens the hub row a moment before it owns the process.

    The Runtime binding is what scopes the sweep, not the machine stamp: a
    Session spawned from the console is stamped with the hub's machine until
    the daemon opens it, and a Runtime that could not reconcile those rows
    would leave an operator watching a Session that ended long ago.
    """
    from brains.control import sessions as sessions_ctl

    runtime = _authorize_runtime(
        principal, runtime_id, operation="runtime.execute", capability=CAP_ORG_WRITE
    )
    for session_id in body.owned_session_ids:
        _assert_session_belongs_to_runtime(principal, runtime, session_id)
    reconciled = sessions_ctl.reconcile_machine_sessions(
        runtime.get("machine_id"),
        body.owned_session_ids,
        runtime_id=runtime.get("id"),
        reason=body.reason,
    )
    return {
        "reconciled": [row["id"] for row in reconciled],
        "owned": body.owned_session_ids,
    }
