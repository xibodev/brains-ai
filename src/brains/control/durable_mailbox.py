"""Durable mailbox identity, attachment, and phonebook controls.

Migration 150 owns the storage boundary. Address-based delivery lives in
:mod:`brains.control.durable_mail`; the legacy Session-addressed path in
:mod:`brains.control.mailbox` remains separate.
"""

from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import hmac
import os
import re
import secrets
import stat
import subprocess
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
    MailboxBindingTransition,
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
_WINDOWS_BINDING_PREFIX = "dpapi-v1:"
MAILBOX_NOTIFICATION_MODES = frozenset({"pull", "turn_boundary", "immediate"})
_TOOL_NOTIFICATION_MODES = {
    "copilot-cli": frozenset({"pull", "turn_boundary"}),
    "claude-code": frozenset({"pull", "turn_boundary"}),
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


def _adapter_provenance(adapter: str) -> str:
    value = (adapter or "").strip()
    canonical_mailbox_tool(value)
    if not value or len(value) > 64:
        raise MailboxValidationError("mailbox adapter provenance is unavailable")
    return value


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
    raw_adapter = _adapter_provenance(adapter)
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


def _process_instance_id(pid: int) -> str | None:
    """Return a PID-reuse-safe process birth marker when the OS exposes one."""
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return f"proc:{fields[19]}"
        except (IndexError, OSError, UnicodeError):
            return None
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "(Get-Process -Id $args[0]).StartTime.ToUniversalTime().Ticks",
                    str(pid),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return f"windows:{completed.stdout.strip()}"
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return f"ps:{completed.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_dpapi(data: bytes, *, protect: bool) -> bytes:
    class DataBlob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = DataBlob()
    windll = ctypes.windll  # type: ignore[attr-defined]
    crypt32 = windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = (
        (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        if protect
        else (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    )
    if not function(*args):
        error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise OSError(error_code, "Windows DPAPI binding protection failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        windll.kernel32.LocalFree(target.data)


def _encode_binding_payload(binding_secret: str) -> str:
    if os.name != "nt":
        return binding_secret
    protected = _windows_dpapi(binding_secret.encode(), protect=True)
    return _WINDOWS_BINDING_PREFIX + base64.b64encode(protected).decode("ascii")


def _decode_binding_payload(payload: str) -> str:
    if not payload.startswith(_WINDOWS_BINDING_PREFIX):
        return payload
    if os.name != "nt":
        raise MailboxValidationError("mailbox binding file is unavailable")
    try:
        protected = base64.b64decode(payload.removeprefix(_WINDOWS_BINDING_PREFIX), validate=True)
        return _windows_dpapi(protected, protect=False).decode()
    except (OSError, UnicodeError, ValueError) as exc:
        raise MailboxValidationError("mailbox binding file is unavailable") from exc


def _windows_system_tool(relative: str) -> str:
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise OSError("Windows system root is unavailable")
    executable = (Path(system_root).resolve(strict=True) / relative).resolve(strict=True)
    try:
        executable.relative_to(Path(system_root).resolve(strict=True))
    except ValueError as exc:
        raise OSError("Windows system tool escaped the system root") from exc
    if not executable.is_file():
        raise OSError("Windows system tool is unavailable")
    return str(executable)


def _windows_current_user_sid() -> str:
    completed = subprocess.run(
        [_windows_system_tool("System32/whoami.exe"), "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    row = next(csv.reader([completed.stdout.strip()]))
    if len(row) < 2 or not row[1].startswith("S-1-"):
        raise OSError("current Windows user SID is unavailable")
    return row[1]


# Windows resolves these principals through OS semantics rather than a user
# grant: LOCAL SYSTEM and the local Administrators group can take ownership of
# any file whatever its DACL says, and OWNER RIGHTS only restates the owner's own
# access. An administrator account therefore cannot be reduced to a single-entry
# DACL, so the boundary requires the owner and rejects any other principal.
WINDOWS_OS_PRINCIPAL_SIDS = frozenset(
    {
        "S-1-3-4",  # OWNER RIGHTS
        "S-1-5-18",  # LOCAL SYSTEM
        "S-1-5-32-544",  # BUILTIN\\Administrators
    }
)


def windows_unexpected_acl_principals(acl_sids: tuple[str, ...], owner_sid: str) -> tuple[str, ...]:
    """Return granted principals that are neither the owner nor OS semantics."""
    return tuple(
        value for value in acl_sids if value != owner_sid and value not in WINDOWS_OS_PRINCIPAL_SIDS
    )


def redact_sid(value: str) -> str:
    """Drop the account authority from a SID, keeping its family and relative id.

    A refusal has to name what it rejected to be actionable, but the identifying
    authority of a local or domain account is not ours to disclose.
    """
    parts = value.split("-")
    if len(parts) <= 5:
        return value
    return "-".join([*parts[:4], "x", parts[-1]])


def _windows_binding_acl_sids(path: Path) -> tuple[str, ...]:
    environment = dict(os.environ)
    environment["BRAINS_BINDING_ACL_PATH"] = str(path)
    completed = subprocess.run(
        [
            _windows_system_tool("System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$acl = Get-Acl -LiteralPath $env:BRAINS_BINDING_ACL_PATH; "
            "$acl.Access | ForEach-Object { "
            "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
            "}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    return tuple(sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()}))


def _windows_secure_binding_file(path: Path) -> None:
    sid = _windows_current_user_sid()
    icacls = _windows_system_tool("System32/icacls.exe")
    subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        [icacls, str(path), "/verify"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    acl_sids = _windows_binding_acl_sids(path)
    # The owner and privileged backup operators are OS semantics, not DACL allow
    # entries. The managed file needs no explicit access principal except its user.
    unexpected = windows_unexpected_acl_principals(acl_sids, sid)
    if sid not in acl_sids:
        raise OSError("mailbox binding file ACL does not grant its owner")
    if unexpected:
        listed = ", ".join(redact_sid(value) for value in unexpected)
        raise OSError(f"mailbox binding file ACL contains an unexpected principal: {listed}")


def _secure_binding_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    _windows_secure_binding_file(path)


def protect_owner_only_bytes(data: bytes) -> tuple[str, bytes]:
    """Protect local recovery bytes with the platform's verified owner boundary."""
    if os.name == "nt":
        return "windows-dpapi", _windows_dpapi(data, protect=True)
    return "posix-owner", data


def unprotect_owner_only_bytes(protection: str, data: bytes) -> bytes:
    """Reverse :func:`protect_owner_only_bytes` for the current user."""
    if protection == "windows-dpapi" and os.name == "nt":
        return _windows_dpapi(data, protect=False)
    if protection == "posix-owner" and os.name != "nt":
        return data
    raise OSError("local recovery bytes are unavailable")


def secure_owner_only_file(path: Path) -> None:
    """Apply the verified owner-only POSIX mode or Windows DACL contract."""
    _secure_binding_file(path)


def secure_owner_only_directory(path: Path) -> None:
    """Apply an owner-only boundary to a managed recovery directory."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    _secure_binding_file(path)


def _replace_managed_binding(path: Path, binding_secret: str) -> None:
    """Owner-only atomic replacement; the secret is never returned or logged."""
    _binding_hash(binding_secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    payload = _encode_binding_payload(binding_secret)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _secure_binding_file(temporary)
        os.replace(temporary, path)
        _secure_binding_file(path)
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
    payload = _encode_binding_payload(binding_secret)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _secure_binding_file(path)
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
    value = _decode_binding_payload(value)
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
        "adapter": row.adapter_provenance,
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
                current.adapter_provenance = mailbox.adapter_provenance
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
                existing.adapter_provenance = mailbox.adapter_provenance
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
                adapter_provenance=mailbox.adapter_provenance,
                last_seen_delivery_id=last_cursor,
            )
            session.add(attachment)
            session.flush()
    except IntegrityError as exc:
        raise MailboxUnavailableError("mailbox unavailable") from exc
    return attachment


def _require_attachment_transition_available(
    session, mailbox: Mailbox, agent: AgentSession
) -> None:
    current = (
        session.query(MailboxAttachment)
        .filter(
            MailboxAttachment.mailbox_id == mailbox.id,
            MailboxAttachment.active_slot == 1,
        )
        .one_or_none()
    )
    if current is None or current.session_id == agent.id:
        return
    current_agent = _lock_agent_session(session, current.session_id)
    if _attachment_session_is_current(session, current_agent):
        raise MailboxUnavailableError("mailbox unavailable")


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
    raw_adapter = _adapter_provenance(tool)
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
            adapter_provenance=raw_adapter,
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
        "adapter": mailbox.adapter_provenance,
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
    _adapter_provenance(tool)
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
            "adapter": mailbox.adapter_provenance,
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
        "adapter": mailbox.adapter_provenance,
        "binding_file": str(path),
        "binding_version": mailbox.binding_key_version,
        "status": mailbox.status,
        "action": action,
    }


def _observed_binding_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return _binding_hash(read_mailbox_binding_file(path, managed_only=True))


def _prepare_binding_transition(
    session,
    mailbox: Mailbox,
    *,
    operation: str,
    path: Path,
    agent: AgentSession,
    notification_mode: str,
    to_hash: str | None,
    to_version: int | None,
) -> None:
    if session.get(MailboxBindingTransition, mailbox.id) is not None:
        raise MailboxUnavailableError("mailbox transition requires reconciliation")
    session.add(
        MailboxBindingTransition(
            mailbox_id=mailbox.id,
            operation=operation,
            from_binding_hash=mailbox.binding_key_hash,
            to_binding_hash=to_hash,
            to_binding_version=to_version,
            binding_file=str(path),
            session_id=agent.id,
            owner_pid=os.getpid(),
            owner_process_instance=_process_instance_id(os.getpid()) or "unavailable",
            notification_mode=notification_mode,
        )
    )


def _reconcile_binding_transition(mailbox_id: int, *, force: bool = False) -> dict[str, Any]:
    with _db_module.SessionLocal() as session:
        transition_query = session.query(MailboxBindingTransition).filter(
            MailboxBindingTransition.mailbox_id == mailbox_id
        )
        if session.get_bind().dialect.name == "postgresql":
            transition = transition_query.with_for_update().one_or_none()
        else:
            session.execute(
                update(MailboxBindingTransition)
                .where(MailboxBindingTransition.mailbox_id == mailbox_id)
                .values(operation=MailboxBindingTransition.operation)
            )
            transition = transition_query.one_or_none()
        mailbox = session.get(Mailbox, mailbox_id)
        if transition is None:
            return {"mailbox_id": mailbox_id, "action": "absent"}
        if not force:
            from brains.control.sessions import _pid_alive

            if _pid_alive(transition.owner_pid):
                live_instance = _process_instance_id(transition.owner_pid)
                if live_instance is None or live_instance == transition.owner_process_instance:
                    return {"mailbox_id": mailbox_id, "action": "in_progress"}
        if mailbox is None or mailbox.workspace_id is None:
            raise MailboxUnavailableError("mailbox transition is unavailable")
        workspace = session.get(Workspace, mailbox.workspace_id)
        agent = session.get(AgentSession, transition.session_id)
        if (
            workspace is None
            or agent is None
            or not mailbox.tool
            or not mailbox.native_tool_session_id
        ):
            raise MailboxUnavailableError("mailbox transition is unavailable")
        expected_path = _managed_binding_path(
            workspace, mailbox.tool, mailbox.native_tool_session_id
        ).resolve()
        path = Path(transition.binding_file).resolve()
        if path != expected_path:
            raise MailboxUnavailableError("mailbox transition is unavailable")
        observed = _observed_binding_hash(path)
        operation = transition.operation
        if operation == "create":
            if observed == transition.to_binding_hash:
                _secure_binding_file(path)
                session.delete(transition)
                session.commit()
                session.refresh(mailbox)
                return _managed_result(mailbox, workspace, path, "created")
            if observed is None:
                session.query(MailboxAttachment).filter(
                    MailboxAttachment.mailbox_id == mailbox.id
                ).delete(synchronize_session=False)
                session.delete(transition)
                session.flush()
                session.delete(mailbox)
                session.commit()
                return {"mailbox_id": mailbox_id, "action": "aborted"}
        elif operation in {"rotate", "recover"}:
            if observed == transition.to_binding_hash:
                _secure_binding_file(path)
                if not hmac.compare_digest(
                    mailbox.binding_key_hash or "", transition.from_binding_hash or ""
                ):
                    raise MailboxUnavailableError("mailbox transition is unavailable")
                mailbox.binding_key_hash = transition.to_binding_hash
                mailbox.binding_key_version = transition.to_binding_version
                mailbox.binding_rotated_at = utc_now()
                mailbox.updated_at = utc_now()
                attachment = _attach_current_session(
                    session,
                    mailbox,
                    agent,
                    notification_mode=transition.notification_mode,
                )
                session.delete(transition)
                session.commit()
                session.refresh(mailbox)
                action = "recovered" if operation == "recover" else "rotated"
                result = _managed_result(mailbox, workspace, path, action)
                result["attachment"] = _attachment_result(attachment)
                return result
            if observed == transition.from_binding_hash or (
                operation == "recover" and observed is None
            ):
                session.delete(transition)
                session.commit()
                return {"mailbox_id": mailbox_id, "action": "aborted"}
        elif operation == "revoke":
            if observed is None:
                if mailbox.binding_key_hash != transition.from_binding_hash:
                    raise MailboxUnavailableError("mailbox transition is unavailable")
                detach_session_mailbox_in_transaction(session, agent.id, reason="binding_revoked")
                mailbox.status = "retired"
                mailbox.retired_at = utc_now()
                mailbox.updated_at = utc_now()
                session.delete(transition)
                session.commit()
                session.refresh(mailbox)
                return _managed_result(mailbox, workspace, path, "revoked")
            if observed == transition.from_binding_hash:
                session.delete(transition)
                session.commit()
                return {"mailbox_id": mailbox_id, "action": "aborted"}
        raise MailboxUnavailableError("mailbox transition requires reconciliation")


def reconcile_managed_mailbox_bindings() -> dict[str, Any]:
    """Converge hash-only intents after an interrupted file/database transition."""
    init_db()
    with _db_module.SessionLocal() as session:
        mailbox_ids = [row[0] for row in session.query(MailboxBindingTransition.mailbox_id).all()]
    results = [_reconcile_binding_transition(mailbox_id) for mailbox_id in mailbox_ids]
    return {"count": len(results), "results": results}


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
    init_db()
    reconcile_managed_mailbox_bindings()
    binding_secret = secrets.token_urlsafe(32)
    mailbox_id: int | None = None
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
            mailbox = session.get(Mailbox, result["mailbox_id"])
            assert mailbox is not None
            _prepare_binding_transition(
                session,
                mailbox,
                operation="create",
                path=path,
                agent=agent,
                notification_mode=notification_mode,
                to_hash=mailbox.binding_key_hash,
                to_version=mailbox.binding_key_version,
            )
            session.commit()
            workspace_id = workspace.id
            mailbox_id = mailbox.id
        _create_managed_binding(path, binding_secret)
        output = _reconcile_binding_transition(mailbox_id, force=True)
        record_agent_mailbox_registration(result, session_id, workspace_id)
        return output
    except BaseException:
        if mailbox_id is not None:
            _reconcile_binding_transition(mailbox_id, force=True)
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
    reconcile_managed_mailbox_bindings()
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
        previous_version = mailbox.binding_key_version or 0
        _require_attachable_agent(session, agent)
        _require_attachment_transition_available(session, mailbox, agent)
        mode = validate_notification_mode(canonical_tool, notification_mode)
        _prepare_binding_transition(
            session,
            mailbox,
            operation="recover" if recover else "rotate",
            path=path,
            agent=agent,
            notification_mode=mode,
            to_hash=new_hash,
            to_version=previous_version + 1,
        )
        session.commit()
        mailbox_id = mailbox.id
    try:
        if recover:
            _create_managed_binding(path, new_secret)
        else:
            _replace_managed_binding(path, new_secret)
    except BaseException:
        outcome = _reconcile_binding_transition(mailbox_id, force=True)
        if outcome["action"] != "aborted":
            return outcome
        raise
    return _reconcile_binding_transition(mailbox_id, force=True)


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
    reconcile_managed_mailbox_bindings()
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
        _prepare_binding_transition(
            session,
            mailbox,
            operation="revoke",
            path=path,
            agent=agent,
            notification_mode="pull",
            to_hash=None,
            to_version=None,
        )
        session.commit()
        mailbox_id = mailbox.id
    path.unlink()
    return _reconcile_binding_transition(mailbox_id, force=True)


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


def resolve_managed_notification_proof(
    workspace_path: str,
    adapter: str,
    native_tool_session_id: str,
    *,
    principal: Principal | None = None,
) -> tuple[str, str, str]:
    """Resolve an existing harness attachment to its owner-only proof.

    This is deliberately not an attach/create path. A generated hook may use
    only an already-current managed mailbox binding, so installing a hook
    cannot guess identity or silently create another Session incarnation.
    """
    resolved = _principal_or_local(principal)
    canonical_tool = canonical_mailbox_tool(adapter)
    native_id = validate_native_tool_session_id(native_tool_session_id)
    init_db()
    with _db_module.SessionLocal() as session:
        workspace = _authorized_workspace(
            session,
            resolved,
            workspace_path=workspace_path,
            capability=CAP_ORG_WRITE,
        )
        mailbox = _find_agent_mailbox(session, workspace.id, canonical_tool, native_id)
        if mailbox is None:
            raise MailboxUnavailableError("mailbox unavailable")
        attachment = (
            session.query(MailboxAttachment)
            .filter(
                MailboxAttachment.mailbox_id == mailbox.id,
                MailboxAttachment.active_slot == 1,
            )
            .one_or_none()
        )
        agent = session.get(AgentSession, attachment.session_id) if attachment else None
        if (
            attachment is None
            or agent is None
            or not _attachment_session_is_current(session, agent)
        ):
            raise MailboxUnavailableError("mailbox unavailable")
        path = _managed_binding_path(workspace, canonical_tool, native_id)
        binding_secret = read_mailbox_binding_file(path, managed_only=True)
        _assert_agent_identity(agent, workspace, resolved, canonical_tool)
        operator_id = resolved.operator_id
        assert operator_id is not None
        _verify_existing_mailbox(
            mailbox,
            owner_operator_id=operator_id,
            binding_hash=_binding_hash(binding_secret),
            address=_mailbox_address(canonical_tool, native_id, workspace.slug),
        )
        return attachment.session_id, binding_secret, attachment.notification_mode


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
    "reconcile_managed_mailbox_bindings",
    "recover_managed_agent_mailbox_binding",
    "require_current_agent_mailbox_in_transaction",
    "register_agent_mailbox",
    "register_agent_mailbox_in_transaction",
    "resolve_managed_notification_proof",
    "resume_agent_mailbox",
    "revoke_managed_agent_mailbox_binding",
    "rotate_managed_agent_mailbox_binding",
    "validate_native_tool_session_id",
    "validate_mailbox_registration_inputs",
    "validate_notification_mode",
]
