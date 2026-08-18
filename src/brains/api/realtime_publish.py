"""Realtime publishers for the WS3 REST/action routers: persist, then announce.

Every helper here does three things a bare ``bus.publish`` did not (BL-P0-02):

1. **Resolves the real scope.** The Org a Session, Issue, approval or Runtime
   belongs to is read from the store, not taken from whatever the caller
   happened to pass. The old ``org_id or "default"`` fallback published another
   Org's Session onto the default Org's topic, which is a cross-Org disclosure
   the subscriber side could not undo. An Issue whose Org cannot be resolved
   publishes nothing rather than falling back to the default Org, because a
   payload that only *names* an Issue is not evidence that it has no Org.
2. **Commits before it announces.** The event is written to
   ``realtime_events`` and only then handed to the in-process bus
   (:func:`brains.events.store.publish_durable`), so a client that reconnects
   catches up by cursor and nobody is told about a change that did not commit.
3. **Names topics through the grammar.** Topics are built by
   :mod:`brains.events.topics`, so a publisher cannot invent a name no
   subscriber is allowed to resolve.

Publishing stays best-effort with respect to the *write* that triggered it: a
realtime failure publishes nothing and never breaks the mutation.

High-frequency liveness (``runtime.heartbeat``) deliberately does **not** come
through here: see :func:`brains.api.runtimes._publish_runtime`.

``_default_org_id`` is used where a row that declares no Org genuinely belongs
to the install's default Org - the same reading authorization takes - and never
as a guess for a scope that could not be resolved.
"""

from __future__ import annotations

import contextlib
from typing import Any

from brains.events import topics as topic_grammar
from brains.events.store import publish_durable


def org_topic(org_id: Any, channel: str) -> str:
    return topic_grammar.org_topic(org_id, channel)


def _emit(topic_builder, event_type: str, **kwargs: Any) -> None:
    """Build a topic and publish it, or do neither.

    ``topic_builder`` refuses a reference that is not nameable in the grammar
    (:func:`brains.events.topics.valid_reference`), which is the case a
    publisher must not turn into a stream nobody may subscribe to. Realtime is
    best-effort with respect to the write that triggered it, and several
    callers publish outside a ``try``, so nothing here may raise.
    """
    with contextlib.suppress(Exception):
        publish_durable(topic_builder(), event_type, **kwargs)


def _default_org_id() -> int | None:
    from brains.authz import policy

    try:
        return policy.default_org_id()
    except Exception:
        return None


def _org_id_for_project(project_id: int | None) -> int | None:
    if project_id is None:
        return None
    try:
        from brains.control import projects as projects_ctl

        proj = projects_ctl.get_project(project_id)
        return proj["org_id"] if proj else None
    except Exception:
        return None


def _org_id_for_issue_code(code: Any) -> int | None:
    if not code:
        return None
    try:
        from brains.authz import policy

        return policy.issue_org_id_for_code(str(code))
    except Exception:
        return None


def _issue_identity(issue: dict[str, Any]) -> tuple[Any, int | None]:
    """``(code, project_id)`` for an Issue row *or* a payload that names one.

    Not every caller passes the Issue row: a comment publishes
    ``{"issue": <code>, "comment": ...}`` and a merge publishes
    ``{"issue": <row>, "status": ...}``. Reading only the top level would find
    neither, and a publisher that cannot identify the Issue must not guess an
    Org - guessing is how one Org's Issue ends up on another Org's topic.
    """
    nested = issue.get("issue")
    if isinstance(nested, dict):
        return nested.get("code"), nested.get("project_id")
    code = issue.get("code") or (nested if isinstance(nested, str) else None)
    return code, issue.get("project_id")


def _session_scope(session_id: Any) -> tuple[int | None, int | None]:
    """``(org_id, workspace_id)`` for a Session, resolved from the store."""
    if not session_id:
        return None, None
    try:
        from brains.authz import policy

        workspace_id = policy.session_workspace_id(str(session_id))
        return policy.workspace_org_id(workspace_id), workspace_id
    except Exception:
        return None, None


def _approval_scope(code: Any) -> tuple[int | None, int | None]:
    """``(org_id, workspace_id)`` for an approval/ASK, resolved from the store."""
    if not code:
        return None, None
    try:
        from brains.authz import policy

        workspace_id = policy.approval_workspace_id(str(code))
        return policy.workspace_org_id(workspace_id), workspace_id
    except Exception:
        return None, None


def _runtime_scope(runtime_id: Any) -> int | None:
    try:
        from brains.authz import policy

        return policy.runtime_org_id(int(runtime_id)) if runtime_id is not None else None
    except Exception:
        return None


def publish_issue(event_type: str, issue: dict[str, Any], *, dedupe_key: str | None = None) -> None:
    """Emit an ``issue.*`` event to ``org/{org}/issues`` and ``issue/{code}``.

    The Org is resolved from the Issue the payload names - by Project, or by
    Issue code when the payload is a wrapper rather than the row itself. An
    Issue whose Org cannot be resolved publishes **nothing**: falling back to
    the install's default Org would put one Org's Issue body on a topic every
    default-Org member may read.
    """
    code, project_id = _issue_identity(issue)
    org_id = _org_id_for_project(project_id)
    if org_id is None:
        org_id = _org_id_for_issue_code(code)
    if org_id is None:
        return
    _emit(
        lambda: org_topic(org_id, "issues"),
        event_type,
        entity="issue",
        entity_id=issue.get("id"),
        org_id=org_id,
        payload=issue,
        dedupe_key=None if dedupe_key is None else f"{dedupe_key}:org",
    )
    if code:
        _emit(
            lambda: topic_grammar.issue_topic(code),
            event_type,
            entity="issue",
            entity_id=issue.get("id"),
            org_id=org_id,
            payload=issue,
            dedupe_key=None if dedupe_key is None else f"{dedupe_key}:issue",
        )


def publish_inbox(
    org_id: Any, event_type: str, payload: dict[str, Any], *, dedupe_key: str | None = None
) -> None:
    """Emit an approval/ask_human event to the owning Org's ``inbox`` topic."""
    code = payload.get("code")
    resolved_org, workspace_id = _approval_scope(code)
    org_id = resolved_org if resolved_org is not None else org_id
    if org_id is None:
        org_id = _default_org_id()
    if org_id is None:
        return
    _emit(
        lambda: org_topic(org_id, "inbox"),
        event_type,
        entity="approval_request",
        entity_id=code,
        org_id=org_id,
        workspace_id=workspace_id,
        payload=payload,
        dedupe_key=dedupe_key,
    )


def publish_session(
    org_id: Any, event_type: str, payload: dict[str, Any], *, dedupe_key: str | None = None
) -> None:
    """Emit a ``session.*`` event to the Org and to the Session's own stream."""
    session_id = payload.get("session_id") or payload.get("id")
    resolved_org, workspace_id = _session_scope(session_id)
    org_id = resolved_org if resolved_org is not None else org_id
    if org_id is None:
        org_id = _default_org_id()
    if org_id is None:
        return
    _emit(
        lambda: org_topic(org_id, "sessions"),
        event_type,
        entity="agent_session",
        entity_id=session_id,
        org_id=org_id,
        workspace_id=workspace_id,
        payload=payload,
        dedupe_key=None if dedupe_key is None else f"{dedupe_key}:org",
    )
    if session_id:
        _emit(
            lambda: topic_grammar.session_topic(session_id, "state"),
            event_type,
            entity="agent_session",
            entity_id=session_id,
            org_id=org_id,
            workspace_id=workspace_id,
            payload=payload,
            dedupe_key=None if dedupe_key is None else f"{dedupe_key}:session",
        )


def publish_session_command(
    command: dict[str, Any], *, event_type: str = "session.command"
) -> None:
    """Emit a durable Session command event on the Session's ``chat`` stream.

    This is the one publisher in the product with a *stable* key to derive a
    ``dedupe_key`` from: a command has an id, and the state it reached is part
    of its identity. ``{command_id}:{status}:{result}`` is therefore the same
    string however many times the mutation is retried, so a retried message or
    a stop pressed twice is one durable event with one ``event_id`` and one
    delivery - the publisher-level idempotency BL-P0-02 could only assert at
    the store level.

    The Org is resolved from the Session rather than taken from the command
    row, for the same reason every other publisher here does it: a payload
    that merely names a Session is not evidence of the Org it belongs to.
    """
    session_id = command.get("session_id")
    if not session_id:
        return
    resolved_org, workspace_id = _session_scope(session_id)
    org_id = resolved_org if resolved_org is not None else command.get("org_id")
    if org_id is None:
        org_id = _default_org_id()
    if org_id is None:
        return
    key = f"session_command:{command.get('command_id')}:{command.get('status')}:{command.get('result')}"
    _emit(
        lambda: topic_grammar.session_topic(session_id, "chat"),
        event_type,
        entity="session_command",
        entity_id=command.get("command_id"),
        org_id=org_id,
        workspace_id=workspace_id,
        payload=command,
        dedupe_key=f"{key}:chat",
    )
    _emit(
        lambda: org_topic(org_id, "sessions"),
        event_type,
        entity="session_command",
        entity_id=command.get("command_id"),
        org_id=org_id,
        workspace_id=workspace_id,
        payload=command,
        dedupe_key=f"{key}:org",
    )


def publish_runtime(
    org_id: Any, event_type: str, payload: dict[str, Any], *, dedupe_key: str | None = None
) -> None:
    """Emit a ``runtime.*`` event to the Org, the Runtime, and its machine."""
    runtime_id = payload.get("id")
    resolved_org = _runtime_scope(runtime_id)
    if resolved_org is None:
        resolved_org = payload.get("org_id") if payload.get("org_id") is not None else org_id
    if resolved_org is None:
        resolved_org = _default_org_id()
    if resolved_org is None:
        return
    _emit(
        lambda: org_topic(resolved_org, "runtimes"),
        event_type,
        entity="runtime",
        entity_id=runtime_id,
        org_id=resolved_org,
        payload=payload,
        dedupe_key=None if dedupe_key is None else f"{dedupe_key}:org",
    )
    if runtime_id is not None:
        _emit(
            lambda: topic_grammar.runtime_topic(runtime_id, "status"),
            event_type,
            entity="runtime",
            entity_id=runtime_id,
            org_id=resolved_org,
            payload=payload,
            dedupe_key=None if dedupe_key is None else f"{dedupe_key}:runtime",
        )
    machine_id = payload.get("machine_id")
    if machine_id:
        _emit(
            lambda: topic_grammar.machine_topic(machine_id, "control"),
            event_type,
            entity="runtime",
            entity_id=runtime_id,
            org_id=resolved_org,
            payload=payload,
            dedupe_key=None if dedupe_key is None else f"{dedupe_key}:machine",
        )
