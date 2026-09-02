"""Durable mailbox identity, attachment, and phonebook controls.

Migration 150 owns the storage boundary. Address-based delivery lives in
:mod:`brains.control.durable_mail`; the legacy Session-addressed path in
:mod:`brains.control.mailbox` remains separate.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from brains.authz import policy
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.authz.resolver import resolve_local_principal
from brains.control.common import utc_now
from brains.control.events import append_event
from brains.storage import db as _db_module
from brains.storage.migrations import init_db
from brains.storage.models import (
    AgentSession,
    Mailbox,
    MailboxAttachment,
    MailNotificationAttempt,
    Operator,
    OrgMember,
    SessionLease,
    Workspace,
    WorkspaceMembership,
)

_TOOL_ALIASES = {
    "copilot": "copilot-cli",
    "copilot-cli": "copilot-cli",
    "github-copilot": "copilot-cli",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "openai-codex": "codex",
    "opencode": "opencode",
}
_NATIVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{11,255}$")
_INVALID_NATIVE_IDS = frozenset(
    {
        "current",
        "default",
        "latest",
        "new",
        "none",
        "null",
        "session",
        "test",
        "unknown",
        *_TOOL_ALIASES,
    }
)
_INVALID_NATIVE_PREFIXES = ("task-", "issue-", "project-")
_MODEL_NAME_RE = re.compile(
    r"^(?:gpt(?:-|$)|o[134](?:-|$)|claude-(?:haiku|opus|sonnet)(?:-|$)|gemini(?:-|$))",
    re.IGNORECASE,
)
_BRAINS_SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{12}$", re.IGNORECASE)
_MANAGED_BINDING_FILE_RE = re.compile(r"^[0-9a-f]{64}\.binding$")
_BINDING_DOMAIN = b"brains-mailbox-binding-v1\0"
MAILBOX_NOTIFICATION_MODES = frozenset({"pull", "turn_boundary", "immediate"})
_TOOL_NOTIFICATION_MODES = {
    "copilot-cli": frozenset({"pull"}),
    "claude-code": frozenset({"pull", "immediate"}),
    "codex": frozenset({"pull", "turn_boundary"}),
    "opencode": frozenset({"pull", "immediate"}),
}
_NATIVE_ID_CONTEXT_KEYS = {
    "copilot-cli": ("copilot_session_id",),
    "claude-code": ("claude_session_id",),
    "codex": ("codex_thread_id", "codex_session_id"),
    "opencode": ("opencode_session_id",),
}


class MailboxError(RuntimeError):
    """Base class for durable mailbox control failures."""


class MailboxValidationError(MailboxError, ValueError):
    """Caller input cannot be a canonical mailbox identity."""


class MailboxUnavailableError(MailboxError, LookupError):
    """Absent, unauthorized, retired, conflicting, or unprovable mailbox."""


def canonical_mailbox_tool(tool: str) -> str:
    """Return a supported canonical harness name or reject it."""
    normalized = (tool or "").strip().lower()
    canonical = _TOOL_ALIASES.get(normalized)
    if canonical is None:
        raise MailboxValidationError(
            "mailbox tool must identify Copilot CLI, Claude Code, Codex, or OpenCode"
        )
    return canonical


def validate_native_tool_session_id(native_tool_session_id: str) -> str:
    """Reject placeholders and labels that cannot be adapter Session IDs."""
    value = (native_tool_session_id or "").strip()
    lowered = value.lower()
    if (
        value != native_tool_session_id
        or not _NATIVE_ID_RE.fullmatch(value)
        or lowered in _INVALID_NATIVE_IDS
        or lowered.startswith(_INVALID_NATIVE_PREFIXES)
        or _MODEL_NAME_RE.match(value)
        or _BRAINS_SESSION_ID_RE.fullmatch(value)
    ):
        raise MailboxValidationError(
            "native_tool_session_id must be the harness adapter's real native Session ID"
        )
    return value


def extract_native_tool_session_id(
    adapter: str,
    context: dict[str, str | None],
) -> dict[str, Any]:
    """Resolve one adapter-native ID without guessing or losing provenance.

    Harness adapters pass only values they obtained from their native protocol.
    Absence and disagreement are explicit results, never fabricated identities.
    """
    raw_adapter = (adapter or "").strip()
    canonical_tool = canonical_mailbox_tool(raw_adapter)
    candidates: list[tuple[str, str]] = []
    for key in _NATIVE_ID_CONTEXT_KEYS[canonical_tool]:
        value = context.get(key)
        if value:
            candidates.append((key, validate_native_tool_session_id(value)))
    distinct = {value for _source, value in candidates}
    if not candidates:
        return {
            "status": "unavailable",
            "adapter": raw_adapter,
            "tool": canonical_tool,
            "native_tool_session_id": None,
            "source": None,
        }
    if len(distinct) != 1:
        return {
            "status": "ambiguous",
            "adapter": raw_adapter,
            "tool": canonical_tool,
            "native_tool_session_id": None,
            "source": None,
        }
    native_id = distinct.pop()
    return {
        "status": "resolved",
        "adapter": raw_adapter,
        "tool": canonical_tool,
        "native_tool_session_id": native_id,
        "source": next(source for source, value in candidates if value == native_id),
    }


def _binding_hash(binding_secret: str) -> str:
    value = binding_secret or ""
    if (
        value != value.strip()
        or any(character.isspace() for character in value)
        or not 32 <= len(value) <= 512
    ):
        raise MailboxValidationError(
            "mailbox binding secret must contain 32-512 non-space characters"
        )
    return hashlib.sha256(_BINDING_DOMAIN + value.encode("utf-8")).hexdigest()


def _managed_binding_path(workspace: Workspace, tool: str, native_id: str) -> Path:
    from brains.api.admin_key import state_dir

    identity = f"{workspace.id}\0{tool}\0{native_id}".encode()
    return state_dir() / "mailbox-bindings" / f"{hashlib.sha256(identity).hexdigest()}.binding"


def _replace_managed_binding(path: Path, binding_secret: str) -> None:
    """Owner-only atomic replacement; the secret is never returned or logged."""
    _binding_hash(binding_secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(binding_secret + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _create_managed_binding(path: Path, binding_secret: str) -> None:
    """Create a managed binding without overwriting a concurrent winner."""
    _binding_hash(binding_secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MailboxUnavailableError("mailbox unavailable") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(binding_secret + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def validate_mailbox_registration_inputs(
    tool: str,
    native_tool_session_id: str,
    binding_secret: str,
) -> tuple[str, str]:
    """Validate registration inputs before any Workspace or Session write."""
    canonical_tool = canonical_mailbox_tool(tool)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    _binding_hash(binding_secret)
    return canonical_tool, native_id


def validate_notification_mode(tool: str, notification_mode: str) -> str:
    """Accept only truthful adapter-declared delivery capabilities."""
    canonical_tool = canonical_mailbox_tool(tool)
    mode = (notification_mode or "").strip().lower()
    if mode not in _TOOL_NOTIFICATION_MODES[canonical_tool]:
        raise MailboxValidationError(f"notification_mode is unavailable for {canonical_tool}")
    return mode


def notification_modes_for_tool(tool: str) -> tuple[str, ...]:
    """The modes a canonical harness adapter can truthfully implement."""
    canonical_tool = canonical_mailbox_tool(tool)
    return tuple(sorted(_TOOL_NOTIFICATION_MODES[canonical_tool]))


def read_mailbox_binding_file(
    binding_file: str | Path,
    *,
    managed_only: bool = False,
) -> str:
    """Read one bounded, owner-only adapter binding file."""
    path = Path(binding_file).expanduser()
    if managed_only:
        from brains.api.admin_key import state_dir

        try:
            managed_root = (state_dir() / "mailbox-bindings").resolve()
            path = path.resolve(strict=True)
        except OSError as exc:
            raise MailboxValidationError("mailbox binding file is unavailable") from exc
        if path.parent != managed_root or not _MANAGED_BINDING_FILE_RE.fullmatch(path.name):
            raise MailboxValidationError("mailbox binding file is outside managed state")
    try:
        with path.open("r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024:
                raise MailboxValidationError("mailbox binding file is unavailable")
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise MailboxValidationError(
                    "mailbox binding file must be readable only by its owner"
                )
            value = handle.read(1025).strip()
    except (OSError, UnicodeError) as exc:
        raise MailboxValidationError("mailbox binding file is unavailable") from exc
    if len(value.encode("utf-8")) > 1024:
        raise MailboxValidationError("mailbox binding file is unavailable")
    _binding_hash(value)
    return value


def _principal_or_local(principal: Principal | None) -> Principal:
    resolved = principal or resolve_local_principal()
    if not resolved.is_operator or resolved.operator_id is None:
        raise MailboxUnavailableError("mailbox unavailable")
    return resolved


def _authorized_workspace(
    session,
    principal: Principal,
    *,
    workspace_path: str | None = None,
    workspace_id: int | None = None,
    capability: str,
) -> Workspace:
    if workspace_id is not None:
        workspace = session.get(Workspace, workspace_id)
    elif workspace_path:
        from brains.control.sessions import _resolve_workspace_path

        workspace = _resolve_workspace_path(session, workspace_path)
    else:
        workspace = None
    if workspace is None or workspace.status != "active":
        raise MailboxUnavailableError("mailbox unavailable")
    if not policy.can_see_workspace(principal, workspace.id) or not principal.has_capability(
        capability, workspace.org_id
    ):
        raise MailboxUnavailableError("mailbox unavailable")
    return workspace


def _ensure_operator_mailbox_row(
    session,
    operator_id: int,
    operator_slug: str,
) -> tuple[Mailbox, bool]:
    address = f"operator:{operator_slug}@brains"
    existing = (
        session.query(Mailbox)
        .filter(Mailbox.owner_operator_id == operator_id, Mailbox.operator_slot == 1)
        .one_or_none()
    )
    if existing is not None:
        if (
            existing.address != address
            or existing.kind != "operator"
            or existing.status != "active"
        ):
            raise MailboxUnavailableError("mailbox unavailable")
        return existing, False
    address_owner = session.query(Mailbox).filter(Mailbox.address == address).one_or_none()
    if address_owner is not None:
        raise MailboxUnavailableError("mailbox unavailable")
    row = Mailbox(
        address=address,
        kind="operator",
        owner_operator_id=operator_id,
        operator_slot=1,
        status="active",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        session.expire_all()
        existing = (
            session.query(Mailbox)
            .filter(Mailbox.owner_operator_id == operator_id, Mailbox.operator_slot == 1)
            .one_or_none()
        )
        if (
            existing is None
            or existing.address != address
            or existing.kind != "operator"
            or existing.status != "active"
        ):
            raise MailboxUnavailableError("mailbox unavailable") from exc
        return existing, False
    return row, True


def ensure_operator_mailboxes() -> list[dict[str, Any]]:
    """Idempotently provision one durable inbox for every stored operator."""
    init_db()
    created: list[dict[str, Any]] = []
    with _db_module.SessionLocal() as session:
        operators = session.query(Operator).order_by(Operator.id.asc()).all()
        for operator in operators:
            row, was_created = _ensure_operator_mailbox_row(session, operator.id, operator.slug)
            if was_created:
                created.append({"mailbox_id": row.id, "address": row.address})
        session.commit()
    return created


def _mailbox_address(tool: str, native_tool_session_id: str, workspace_slug: str) -> str:
    address = f"{tool}:{native_tool_session_id}@{workspace_slug}"
    if len(address) > 512:
        raise MailboxValidationError("mailbox address exceeds 512 characters")
    return address


def _verify_existing_mailbox(
    row: Mailbox,
    *,
    owner_operator_id: int,
    binding_hash: str,
    address: str,
) -> None:
    if (
        row.kind != "agent"
        or row.status != "active"
        or row.owner_operator_id != owner_operator_id
        or row.address != address
        or not row.binding_key_hash
        or not hmac.compare_digest(row.binding_key_hash, binding_hash)
    ):
        raise MailboxUnavailableError("mailbox unavailable")


def _assert_agent_identity(
    agent: AgentSession,
    workspace: Workspace,
    resolved: Principal,
    canonical_tool: str,
) -> None:
    if agent.runtime_id is not None or agent.persona_id is not None or agent.issue_id is not None:
        raise MailboxUnavailableError("mailbox unavailable")
    try:
        session_tool = canonical_mailbox_tool(agent.tool)
    except MailboxValidationError as exc:
        raise MailboxUnavailableError("mailbox unavailable") from exc
    if (
        agent.workspace_id != workspace.id
        or agent.created_by_operator_id != resolved.operator_id
        or session_tool != canonical_tool
    ):
        raise MailboxUnavailableError("mailbox unavailable")


def _require_attachable_agent(session, agent: AgentSession) -> None:
    if agent.ended_at is not None or agent.state in {"dormant", "completed", "failed"}:
        raise MailboxUnavailableError("mailbox unavailable")
    if agent.pid is None:
        from brains.control.session_liveness import lease_is_current

        lease = session.get(SessionLease, agent.id)
        if lease is None or not lease_is_current(lease):
            raise MailboxUnavailableError("mailbox unavailable")


def _find_agent_mailbox(
    session,
    workspace_id: int,
    canonical_tool: str,
    native_id: str,
) -> Mailbox | None:
    return (
        session.query(Mailbox)
        .filter(
            Mailbox.workspace_id == workspace_id,
            Mailbox.tool == canonical_tool,
            Mailbox.native_tool_session_id == native_id,
        )
        .one_or_none()
    )


def _lock_agent_session(session, session_id: str) -> AgentSession | None:
    query = session.query(AgentSession).filter(AgentSession.id == session_id)
    if session.get_bind().dialect.name == "postgresql":
        return query.with_for_update().one_or_none()
    session.execute(
        update(AgentSession).where(AgentSession.id == session_id).values(state=AgentSession.state)
    )
    return query.one_or_none()


def _attachment_session_is_current(session, agent: AgentSession | None) -> bool:
    if (
        agent is None
        or agent.ended_at is not None
        or agent.state
        in {
            "dormant",
            "completed",
            "failed",
        }
    ):
        return False
    if agent.pid is not None:
        return True
    from brains.control.session_liveness import lease_is_current

    return lease_is_current(session.get(SessionLease, agent.id))


def mailbox_attachment_is_current_in_transaction(session, agent: AgentSession) -> bool:
    """Whether ``agent`` is the still-reachable current mailbox incarnation."""
    attachment = (
        session.query(MailboxAttachment.id)
        .filter(MailboxAttachment.session_id == agent.id, MailboxAttachment.active_slot == 1)
        .first()
    )
    return attachment is not None and _attachment_session_is_current(session, agent)


def prove_session_mailbox_binding_in_transaction(
    session,
    workspace: Workspace,
    agent: AgentSession,
    *,
    tool: str | None,
    native_tool_session_id: str | None,
    binding_secret: str | None,
    principal: Principal | None = None,
) -> int | None:
    """Require proof when ``agent`` has durable mailbox attachment history."""
    attachment = (
        session.query(MailboxAttachment)
        .filter(MailboxAttachment.session_id == agent.id)
        .one_or_none()
    )
    if attachment is None:
        return None
    if not tool or not native_tool_session_id or not binding_secret:
        raise MailboxUnavailableError("mailbox unavailable")
    resolved = _principal_or_local(principal)
    if not policy.can_see_workspace(resolved, workspace.id) or not resolved.has_capability(
        CAP_ORG_WRITE, workspace.org_id
    ):
        raise MailboxUnavailableError("mailbox unavailable")
    canonical_tool = canonical_mailbox_tool(tool)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    _assert_agent_identity(agent, workspace, resolved, canonical_tool)
    mailbox = session.get(Mailbox, attachment.mailbox_id)
    if mailbox is None or mailbox.workspace_id != workspace.id:
        raise MailboxUnavailableError("mailbox unavailable")
    operator_id = resolved.operator_id
    assert operator_id is not None
    _verify_existing_mailbox(
        mailbox,
        owner_operator_id=operator_id,
        binding_hash=_binding_hash(binding_secret),
        address=_mailbox_address(canonical_tool, native_id, workspace.slug),
    )
    return mailbox.id


def require_current_agent_mailbox_in_transaction(
    session,
    session_id: str,
    binding_secret: str,
    *,
    address: str | None = None,
    capability: str = CAP_ORG_READ,
    principal: Principal | None = None,
) -> tuple[Mailbox, MailboxAttachment, AgentSession, Workspace]:
    """Resolve one proof-bound current agent mailbox without renewing reachability."""
    resolved = _principal_or_local(principal)
    agent = _lock_agent_session(session, session_id)
    if agent is None:
        raise MailboxUnavailableError("mailbox unavailable")
    attachment_query = session.query(MailboxAttachment).filter(
        MailboxAttachment.session_id == session_id,
        MailboxAttachment.active_slot == 1,
    )
    if session.get_bind().dialect.name == "postgresql":
        attachment_query = attachment_query.with_for_update()
    attachment = attachment_query.one_or_none()
    if attachment is None or not _attachment_session_is_current(session, agent):
        raise MailboxUnavailableError("mailbox unavailable")
    mailbox = session.get(Mailbox, attachment.mailbox_id)
    workspace = session.get(Workspace, agent.workspace_id) if agent.workspace_id else None
    if (
        mailbox is None
        or mailbox.kind != "agent"
        or workspace is None
        or workspace.status != "active"
        or mailbox.workspace_id != workspace.id
        or (address is not None and mailbox.address != address.strip())
        or not policy.can_see_workspace(resolved, workspace.id)
        or not resolved.has_capability(capability, workspace.org_id)
    ):
        raise MailboxUnavailableError("mailbox unavailable")
    try:
        canonical_tool = canonical_mailbox_tool(mailbox.tool or "")
        native_id = validate_native_tool_session_id(mailbox.native_tool_session_id or "")
        binding_hash = _binding_hash(binding_secret)
    except MailboxValidationError as exc:
        raise MailboxUnavailableError("mailbox unavailable") from exc
    _assert_agent_identity(agent, workspace, resolved, canonical_tool)
    operator_id = resolved.operator_id
    assert operator_id is not None
    _verify_existing_mailbox(
        mailbox,
        owner_operator_id=operator_id,
        binding_hash=binding_hash,
        address=_mailbox_address(canonical_tool, native_id, workspace.slug),
    )
    return mailbox, attachment, agent, workspace


def _attachment_result(row: MailboxAttachment) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "notification_mode": row.notification_mode,
        "cursor": row.last_seen_delivery_id,
        "attached_at": row.attached_at.isoformat(),
    }


def _fail_attachment_notifications(
    session,
    attachment: MailboxAttachment,
    *,
    reason: str,
    include_claimed: bool,
) -> None:
    statuses = ("queued", "claimed") if include_claimed else ("queued",)
    now = utc_now()
    rows = (
        session.query(MailNotificationAttempt)
        .filter(
            MailNotificationAttempt.attachment_id == attachment.id,
            MailNotificationAttempt.status.in_(statuses),
        )
        .all()
    )
    for row in rows:
        if row.status == "queued":
            row.attempt = 1
            row.started_at = now
        row.status = "failed"
        row.error_code = reason
        row.completed_at = now


def _attach_current_session(
    session,
    mailbox: Mailbox,
    agent: AgentSession,
    *,
    notification_mode: str,
) -> MailboxAttachment:
    try:
        with session.begin_nested():
            current_query = session.query(MailboxAttachment).filter(
                MailboxAttachment.mailbox_id == mailbox.id,
                MailboxAttachment.active_slot == 1,
            )
            if session.get_bind().dialect.name == "postgresql":
                current_query = current_query.with_for_update()
            current = current_query.one_or_none()
            if current is not None and current.session_id == agent.id:
                if current.notification_mode != notification_mode:
                    _fail_attachment_notifications(
                        session,
                        current,
                        reason="notification_mode_changed",
                        include_claimed=False,
                    )
                current.notification_mode = notification_mode
                return current
            if current is not None:
                current_agent = _lock_agent_session(session, current.session_id)
                if _attachment_session_is_current(session, current_agent):
                    raise MailboxUnavailableError("mailbox unavailable")
                current.active_slot = None
                current.detached_at = utc_now()
                current.detach_reason = "replaced"

            existing = (
                session.query(MailboxAttachment)
                .filter(MailboxAttachment.session_id == agent.id)
                .one_or_none()
            )
            if existing is not None:
                if existing.mailbox_id != mailbox.id:
                    raise MailboxUnavailableError("mailbox unavailable")
                existing.active_slot = 1
                existing.notification_mode = notification_mode
                existing.detached_at = None
                existing.detach_reason = None
                session.flush()
                return existing

            last_cursor = (
                session.query(MailboxAttachment.last_seen_delivery_id)
                .filter(MailboxAttachment.mailbox_id == mailbox.id)
                .order_by(MailboxAttachment.last_seen_delivery_id.desc())
                .limit(1)
                .scalar()
                or 0
            )
            attachment = MailboxAttachment(
                mailbox_id=mailbox.id,
                session_id=agent.id,
                active_slot=1,
                notification_mode=notification_mode,
                last_seen_delivery_id=last_cursor,
            )
            session.add(attachment)
            session.flush()
    except IntegrityError as exc:
        raise MailboxUnavailableError("mailbox unavailable") from exc
    return attachment


def register_agent_mailbox_in_transaction(
    session,
    workspace: Workspace,
    agent: AgentSession,
    tool: str,
    native_tool_session_id: str,
    binding_secret: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Create/find and attach a mailbox without committing the caller's transaction."""
    raw_adapter = (tool or "").strip()
    canonical_tool = canonical_mailbox_tool(tool)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    mode = validate_notification_mode(canonical_tool, notification_mode)
    binding_hash = _binding_hash(binding_secret)
    resolved = _principal_or_local(principal)
    if not policy.can_see_workspace(resolved, workspace.id) or not resolved.has_capability(
        CAP_ORG_WRITE, workspace.org_id
    ):
        raise MailboxUnavailableError("mailbox unavailable")
    _assert_agent_identity(agent, workspace, resolved, canonical_tool)

    operator_id = resolved.operator_id
    assert operator_id is not None
    operator = session.get(Operator, operator_id)
    if operator is None:
        raise MailboxUnavailableError("mailbox unavailable")
    _ensure_operator_mailbox_row(session, operator.id, operator.slug)
    address = _mailbox_address(canonical_tool, native_id, workspace.slug)
    mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
    if mailbox is not None:
        _verify_existing_mailbox(
            mailbox,
            owner_operator_id=operator.id,
            binding_hash=binding_hash,
            address=address,
        )
    _require_attachable_agent(session, agent)
    created = False
    if mailbox is None:
        if session.query(Mailbox.id).filter(Mailbox.address == address).first() is not None:
            raise MailboxUnavailableError("mailbox unavailable")
        candidate = Mailbox(
            address=address,
            kind="agent",
            workspace_id=workspace.id,
            tool=canonical_tool,
            native_tool_session_id=native_id,
            owner_operator_id=operator.id,
            binding_key_hash=binding_hash,
            binding_key_version=1,
            status="active",
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            mailbox = candidate
            created = True
        except IntegrityError:
            mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
            if mailbox is None:
                raise MailboxUnavailableError("mailbox unavailable") from None
            _verify_existing_mailbox(
                mailbox,
                owner_operator_id=operator.id,
                binding_hash=binding_hash,
                address=address,
            )
    attachment = _attach_current_session(
        session,
        mailbox,
        agent,
        notification_mode=mode,
    )
    session.flush()
    from brains.control.durable_mail import (
        _queue_unread_notifications_in_transaction,
        unread_mailbox_count_in_transaction,
    )

    _queue_unread_notifications_in_transaction(session, mailbox)
    unread_count = unread_mailbox_count_in_transaction(
        session,
        mailbox.id,
        principal=resolved,
    )
    return {
        "mailbox_id": mailbox.id,
        "address": mailbox.address,
        "kind": mailbox.kind,
        "status": mailbox.status,
        "workspace": workspace.slug,
        "tool": mailbox.tool,
        "adapter": raw_adapter,
        "attachment": _attachment_result(attachment),
        "unread_count": unread_count,
        "created": created,
    }


def record_agent_mailbox_registration(
    result: dict[str, Any], session_id: str, workspace_id: int
) -> None:
    created = bool(result["created"])
    append_event(
        "mailbox_registered" if created else "mailbox_attached",
        f"durable mailbox {'registered' if created else 'attached'}",
        workspace_id=workspace_id,
        session_id=session_id,
        metadata={
            "tool": result["tool"],
            "adapter": result.get("adapter", result["tool"]),
            "result": "success",
        },
    )


def register_agent_mailbox(
    workspace_path: str,
    tool: str,
    native_tool_session_id: str,
    session_id: str,
    binding_secret: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Create/find one durable address and attach its current Brains Session."""
    resolved = _principal_or_local(principal)
    init_db()
    with _db_module.SessionLocal() as session:
        workspace = _authorized_workspace(
            session,
            resolved,
            workspace_path=workspace_path,
            capability=CAP_ORG_WRITE,
        )
        agent = _lock_agent_session(session, session_id)
        if agent is None:
            raise MailboxUnavailableError("mailbox unavailable")
        result = register_agent_mailbox_in_transaction(
            session,
            workspace,
            agent,
            tool,
            native_tool_session_id,
            binding_secret,
            notification_mode=notification_mode,
            principal=resolved,
        )
        session.commit()
        workspace_id = workspace.id
    record_agent_mailbox_registration(result, session_id, workspace_id)
    return result


def resume_agent_mailbox(
    workspace_path: str,
    tool: str,
    native_tool_session_id: str,
    session_id: str,
    binding_secret: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Verify binding, renew the Session, and reattach as one transaction."""
    raw_adapter = (tool or "").strip()
    canonical_tool = canonical_mailbox_tool(tool)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    mode = validate_notification_mode(canonical_tool, notification_mode)
    binding_hash = _binding_hash(binding_secret)
    resolved = _principal_or_local(principal)
    operator_id = resolved.operator_id
    assert operator_id is not None
    init_db()
    with _db_module.SessionLocal() as session:
        workspace = _authorized_workspace(
            session,
            resolved,
            workspace_path=workspace_path,
            capability=CAP_ORG_WRITE,
        )
        agent = _lock_agent_session(session, session_id)
        if agent is None:
            raise MailboxUnavailableError("mailbox unavailable")
        _assert_agent_identity(agent, workspace, resolved, canonical_tool)
        address = _mailbox_address(canonical_tool, native_id, workspace.slug)
        mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
        if mailbox is None:
            raise MailboxUnavailableError("mailbox unavailable")
        _verify_existing_mailbox(
            mailbox,
            owner_operator_id=operator_id,
            binding_hash=binding_hash,
            address=address,
        )
        if agent.ended_at is not None or agent.state in {"completed", "failed"}:
            raise MailboxUnavailableError("mailbox unavailable")
        from brains.control.session_liveness import renew_session_lease

        try:
            lease = renew_session_lease(session, agent, mailbox_verified=True)
        except ValueError as exc:
            raise MailboxUnavailableError("mailbox unavailable") from exc
        if agent.pid is None and lease is None:
            raise MailboxUnavailableError("mailbox unavailable")
        attachment = _attach_current_session(
            session,
            mailbox,
            agent,
            notification_mode=mode,
        )
        from brains.control.durable_mail import (
            _queue_unread_notifications_in_transaction,
            unread_mailbox_count_in_transaction,
        )

        _queue_unread_notifications_in_transaction(session, mailbox)
        unread_count = unread_mailbox_count_in_transaction(
            session,
            mailbox.id,
            principal=resolved,
        )
        session.commit()
        session.refresh(mailbox)
        session.refresh(attachment)
        result = {
            "mailbox_id": mailbox.id,
            "address": mailbox.address,
            "kind": mailbox.kind,
            "status": mailbox.status,
            "workspace": workspace.slug,
            "tool": mailbox.tool,
            "adapter": raw_adapter,
            "attachment": _attachment_result(attachment),
            "unread_count": unread_count,
            "created": False,
        }
        workspace_id = workspace.id
    record_agent_mailbox_registration(result, session_id, workspace_id)
    return result


def _managed_result(
    mailbox: Mailbox, workspace: Workspace, path: Path, action: str
) -> dict[str, Any]:
    return {
        "mailbox_id": mailbox.id,
        "address": mailbox.address,
        "workspace": workspace.slug,
        "tool": mailbox.tool,
        "binding_file": str(path),
        "binding_version": mailbox.binding_key_version,
        "status": mailbox.status,
        "action": action,
    }


def create_managed_agent_mailbox(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    session_id: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Atomically provision an owner-only binding file and mailbox registration."""
    resolved = _principal_or_local(principal)
    canonical_tool = canonical_mailbox_tool(adapter)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    binding_secret = secrets.token_urlsafe(32)
    init_db()
    path: Path | None = None
    binding_written = False
    try:
        with _db_module.SessionLocal() as session:
            workspace = _authorized_workspace(
                session, resolved, workspace_path=workspace_path, capability=CAP_ORG_WRITE
            )
            agent = _lock_agent_session(session, session_id)
            if agent is None:
                raise MailboxUnavailableError("mailbox unavailable")
            path = _managed_binding_path(workspace, canonical_tool, native_id)
            if path.exists():
                raise MailboxUnavailableError("mailbox unavailable")
            result = register_agent_mailbox_in_transaction(
                session,
                workspace,
                agent,
                adapter,
                native_id,
                binding_secret,
                notification_mode=notification_mode,
                principal=resolved,
            )
            _create_managed_binding(path, binding_secret)
            binding_written = True
            try:
                session.commit()
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            binding_written = False
            mailbox = session.get(Mailbox, result["mailbox_id"])
            assert mailbox is not None
            output = _managed_result(mailbox, workspace, path, "created")
            workspace_id = workspace.id
        record_agent_mailbox_registration(result, session_id, workspace_id)
        return output
    except BaseException:
        if path is not None and binding_written:
            path.unlink(missing_ok=True)
        raise


def _rotate_or_recover_managed_binding(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    session_id: str,
    *,
    recover: bool,
    notification_mode: str,
    principal: Principal | None,
) -> dict[str, Any]:
    resolved = _principal_or_local(principal)
    canonical_tool = canonical_mailbox_tool(adapter)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    new_secret = secrets.token_urlsafe(32)
    new_hash = _binding_hash(new_secret)
    init_db()
    with _db_module.SessionLocal() as session:
        workspace = _authorized_workspace(
            session, resolved, workspace_path=workspace_path, capability=CAP_ORG_WRITE
        )
        agent = _lock_agent_session(session, session_id)
        mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
        if agent is None or mailbox is None:
            raise MailboxUnavailableError("mailbox unavailable")
        _assert_agent_identity(agent, workspace, resolved, canonical_tool)
        operator_id = resolved.operator_id
        assert operator_id is not None
        if mailbox.owner_operator_id != operator_id or mailbox.status != "active":
            raise MailboxUnavailableError("mailbox unavailable")
        path = _managed_binding_path(workspace, canonical_tool, native_id)
        old_secret: str | None = None
        if recover:
            if path.exists():
                raise MailboxUnavailableError("mailbox unavailable")
        else:
            old_secret = read_mailbox_binding_file(path, managed_only=True)
            _verify_existing_mailbox(
                mailbox,
                owner_operator_id=operator_id,
                binding_hash=_binding_hash(old_secret),
                address=_mailbox_address(canonical_tool, native_id, workspace.slug),
            )
        previous_hash = mailbox.binding_key_hash
        previous_version = mailbox.binding_key_version or 0
        changed = session.execute(
            update(Mailbox)
            .where(
                Mailbox.id == mailbox.id,
                Mailbox.binding_key_hash == previous_hash,
                Mailbox.binding_key_version == previous_version,
                Mailbox.status == "active",
            )
            .values(
                binding_key_hash=new_hash,
                binding_key_version=previous_version + 1,
                binding_rotated_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        if getattr(changed, "rowcount", None) != 1:
            raise MailboxUnavailableError("mailbox unavailable")
        _require_attachable_agent(session, agent)
        attachment = _attach_current_session(
            session,
            mailbox,
            agent,
            notification_mode=validate_notification_mode(canonical_tool, notification_mode),
        )
        _replace_managed_binding(path, new_secret)
        try:
            session.commit()
        except BaseException:
            if old_secret is None:
                path.unlink(missing_ok=True)
            else:
                _replace_managed_binding(path, old_secret)
            raise
        session.refresh(mailbox)
        action = "recovered" if recover else "rotated"
        output = _managed_result(mailbox, workspace, path, action)
        output["attachment"] = _attachment_result(attachment)
        return output


def rotate_managed_agent_mailbox_binding(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    session_id: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    return _rotate_or_recover_managed_binding(
        workspace_path,
        adapter,
        native_tool_session_id,
        session_id,
        recover=False,
        notification_mode=notification_mode,
        principal=principal,
    )


def recover_managed_agent_mailbox_binding(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    session_id: str,
    *,
    notification_mode: str = "pull",
    principal: Principal | None = None,
) -> dict[str, Any]:
    return _rotate_or_recover_managed_binding(
        workspace_path,
        adapter,
        native_tool_session_id,
        session_id,
        recover=True,
        notification_mode=notification_mode,
        principal=principal,
    )


def revoke_managed_agent_mailbox_binding(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    session_id: str,
    *,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Proof-bound revocation of a managed mailbox identity."""
    resolved = _principal_or_local(principal)
    canonical_tool = canonical_mailbox_tool(adapter)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    init_db()
    with _db_module.SessionLocal() as session:
        workspace = _authorized_workspace(
            session, resolved, workspace_path=workspace_path, capability=CAP_ORG_WRITE
        )
        agent = _lock_agent_session(session, session_id)
        mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
        if agent is None or mailbox is None:
            raise MailboxUnavailableError("mailbox unavailable")
        path = _managed_binding_path(workspace, canonical_tool, native_id)
        binding_secret = read_mailbox_binding_file(path, managed_only=True)
        prove_session_mailbox_binding_in_transaction(
            session,
            workspace,
            agent,
            tool=canonical_tool,
            native_tool_session_id=native_id,
            binding_secret=binding_secret,
            principal=resolved,
        )
        detach_session_mailbox_in_transaction(session, session_id, reason="binding_revoked")
        changed = session.execute(
            update(Mailbox)
            .where(
                Mailbox.id == mailbox.id,
                Mailbox.binding_key_hash == _binding_hash(binding_secret),
                Mailbox.status == "active",
            )
            .values(status="retired", retired_at=utc_now(), updated_at=utc_now())
        )
        if getattr(changed, "rowcount", None) != 1:
            raise MailboxUnavailableError("mailbox unavailable")
        path.unlink()
        try:
            session.commit()
        except BaseException:
            _replace_managed_binding(path, binding_secret)
            raise
        session.refresh(mailbox)
        return _managed_result(mailbox, workspace, path, "revoked")


def detach_session_mailbox_in_transaction(
    session,
    session_id: str,
    *,
    reason: str,
) -> bool:
    """Detach an active incarnation inside its Session lifecycle transaction."""
    attachment = (
        session.query(MailboxAttachment)
        .filter(MailboxAttachment.session_id == session_id, MailboxAttachment.active_slot == 1)
        .one_or_none()
    )
    if attachment is None:
        return False
    _fail_attachment_notifications(
        session,
        attachment,
        reason="attachment_detached",
        include_claimed=True,
    )
    now = utc_now()
    attachment.active_slot = None
    attachment.detached_at = now
    attachment.detach_reason = (reason or "session_ended")[:64]
    return True


def detach_session_mailbox(session_id: str, *, reason: str = "session_ended") -> dict[str, Any]:
    """Idempotently detach one Session incarnation from its durable mailbox."""
    init_db()
    with _db_module.SessionLocal() as session:
        detached = detach_session_mailbox_in_transaction(session, session_id, reason=reason)
        session.commit()
    return {"session_id": session_id, "detached": detached}


def _operator_mailbox_visible(
    session,
    principal: Principal,
    owner_operator_id: int,
    *,
    workspace: Workspace | None = None,
) -> bool:
    if principal.is_bootstrap_admin or principal.operator_id == owner_operator_id:
        return True
    visible = policy.visible_workspace_ids(principal)
    if visible is None:
        return True
    if workspace is not None:
        workspaces = [workspace] if workspace.id in visible else []
    elif visible:
        workspaces = session.query(Workspace).filter(Workspace.id.in_(visible)).all()
    else:
        workspaces = []
    org_ids = {row.org_id for row in workspaces if row.org_id is not None}
    if not org_ids:
        return False
    owner = session.get(Operator, owner_operator_id)
    if owner is None:
        return False
    if owner.slug == "admin":
        return True
    owner_org_ids = {
        row[0]
        for row in session.query(OrgMember.org_id)
        .filter(OrgMember.operator_id == owner_operator_id, OrgMember.org_id.in_(org_ids))
        .all()
    }
    if not owner_org_ids:
        return False
    owner_private_ids = {
        row[0]
        for row in session.query(WorkspaceMembership.workspace_id)
        .filter(WorkspaceMembership.operator_id == owner_operator_id)
        .all()
    }
    return any(
        candidate.status == "active"
        and candidate.org_id in owner_org_ids
        and (candidate.visibility != "private" or candidate.id in owner_private_ids)
        for candidate in workspaces
    )


def _can_display_path(principal: Principal, workspace: Workspace) -> bool:
    return principal.is_bootstrap_admin or principal.role_in_org(workspace.org_id) in {
        "admin",
        "owner",
    }


def _phonebook_row(
    session,
    mailbox: Mailbox,
    principal: Principal,
    *,
    include_paths: bool,
) -> dict[str, Any]:
    workspace = session.get(Workspace, mailbox.workspace_id) if mailbox.workspace_id else None
    owner = session.get(Operator, mailbox.owner_operator_id)
    result: dict[str, Any] = {
        "address": mailbox.address,
        "kind": mailbox.kind,
        "workspace": workspace.slug if workspace is not None else None,
        "tool": mailbox.tool,
        "owner_operator": owner.slug if owner is not None else None,
    }
    if include_paths and workspace is not None and _can_display_path(principal, workspace):
        result["workspace_path"] = workspace.path
    return result


def list_phonebook(
    workspace_path: str | None = None,
    *,
    include_paths: bool = False,
    principal: Principal | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List only active addresses visible to the resolved principal."""
    resolved = _principal_or_local(principal)
    init_db()
    with _db_module.SessionLocal() as session:
        selected_workspace = None
        if workspace_path is not None:
            selected_workspace = _authorized_workspace(
                session,
                resolved,
                workspace_path=workspace_path,
                capability=CAP_ORG_READ,
            )
        visible = policy.visible_workspace_ids(resolved)
        query = session.query(Mailbox).filter(Mailbox.status == "active")
        cap = max(1, min(int(limit), 1000))
        rows = query.order_by(Mailbox.address.asc()).yield_per(200)
        out: list[dict[str, Any]] = []
        for mailbox in rows:
            if mailbox.kind == "agent":
                if mailbox.workspace_id is None:
                    continue
                workspace = session.get(Workspace, mailbox.workspace_id)
                if workspace is None or workspace.status != "active":
                    continue
                if selected_workspace is not None and mailbox.workspace_id != selected_workspace.id:
                    continue
                if visible is not None and mailbox.workspace_id not in visible:
                    continue
            elif mailbox.kind == "operator":
                if not _operator_mailbox_visible(
                    session,
                    resolved,
                    mailbox.owner_operator_id,
                    workspace=selected_workspace,
                ):
                    continue
            else:
                continue
            out.append(
                _phonebook_row(
                    session,
                    mailbox,
                    resolved,
                    include_paths=include_paths,
                )
            )
            if len(out) >= cap:
                break
        return out


def lookup_mailbox(
    address: str,
    *,
    include_path: bool = False,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Resolve one visible active address; every other case is identical."""
    resolved = _principal_or_local(principal)
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox = (
            session.query(Mailbox).filter(Mailbox.address == (address or "").strip()).one_or_none()
        )
        if mailbox is None or mailbox.status != "active":
            raise MailboxUnavailableError("mailbox unavailable")
        if mailbox.kind == "agent":
            workspace = (
                session.get(Workspace, mailbox.workspace_id) if mailbox.workspace_id else None
            )
            if (
                workspace is None
                or workspace.status != "active"
                or not policy.can_see_workspace(resolved, mailbox.workspace_id)
            ):
                raise MailboxUnavailableError("mailbox unavailable")
        elif mailbox.kind == "operator":
            if not _operator_mailbox_visible(session, resolved, mailbox.owner_operator_id):
                raise MailboxUnavailableError("mailbox unavailable")
        else:
            raise MailboxUnavailableError("mailbox unavailable")
        return _phonebook_row(
            session,
            mailbox,
            resolved,
            include_paths=include_path,
        )


__all__ = [
    "MailboxError",
    "MailboxUnavailableError",
    "MailboxValidationError",
    "canonical_mailbox_tool",
    "create_managed_agent_mailbox",
    "detach_session_mailbox",
    "detach_session_mailbox_in_transaction",
    "ensure_operator_mailboxes",
    "extract_native_tool_session_id",
    "list_phonebook",
    "lookup_mailbox",
    "notification_modes_for_tool",
    "mailbox_attachment_is_current_in_transaction",
    "prove_session_mailbox_binding_in_transaction",
    "record_agent_mailbox_registration",
    "read_mailbox_binding_file",
    "recover_managed_agent_mailbox_binding",
    "require_current_agent_mailbox_in_transaction",
    "register_agent_mailbox",
    "register_agent_mailbox_in_transaction",
    "resume_agent_mailbox",
    "revoke_managed_agent_mailbox_binding",
    "rotate_managed_agent_mailbox_binding",
    "validate_native_tool_session_id",
    "validate_mailbox_registration_inputs",
    "validate_notification_mode",
]
