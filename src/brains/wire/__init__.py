"""``brains wire`` — auto-discovery: register brains into agentic AI tools.

This is the soft-wiring layer of the brains adoption story. For every
detected agentic CLI/IDE on the machine it idempotently injects:

1. **An MCP server entry** so the tool can *reach* every ``brains.*`` tool
   (the capability surface). Default transport is Streamable HTTP — point the
   tool at the supervised ``/mcp`` endpoint — with stdio as an opt-in
   alternative.
2. **A short "use brains first" rule** into the tool's instruction file,
   wrapped in sentinels so it can be updated or removed cleanly (the
   strongest *soft* nudge toward treating brains as mandatory).

There is no universal discovery bus — each tool has its own config
contract — so this is a maintained adapter registry. Tier-1 today:
GitHub Copilot CLI, Claude Code, OpenAI Codex CLI, OpenCode.

Every function is keyed on an explicit ``home`` directory so the whole
surface is unit-testable and exercisable inside a clean-slate Docker
sandbox without touching the operator's real configs. Files are backed up
(``*.bak-<ts>``) before any edit, and a pre-existing non-managed ``brains``
entry is never clobbered.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from brains.config import _canonical_default_db_url
from brains.mcp.transport import (
    MCP_CLIENT_BEARER_ENV,
    MCP_MODE_SSE,
    MCP_MODE_STDIO,
    MCP_MODE_STREAMABLE_HTTP,
    mcp_http_url,
)

# --- defaults -------------------------------------------------------------

DEFAULT_MCP_PORT = 9877
DEFAULT_MCP_HOST = "127.0.0.1"
SERVER_KEY = "brains"

# Sentinels that bound the managed block inside instruction files (markdown)
# and Codex's TOML. Anything outside them is the operator's; we only ever
# touch what is between them.
MD_START = "<!-- brains:wire:start (managed by `brains wire`; do not edit inside) -->"
MD_END = "<!-- brains:wire:end -->"
TOML_START = "# >>> brains:wire:start (managed by `brains wire`; do not edit inside) >>>"
TOML_END = "# <<< brains:wire:end <<<"

# The "use brains first" rule injected into each tool's instruction file.
# Clean-slate, brains-v2 specific — deliberately short so it survives at the
# top of a busy instructions file.
RULE_BODY = """## Brains — shared coordination plane (use the right amount)

Brains coordinates every AI coding session on this machine through one shared
database; the `brains_*` MCP tools and the `brains-ai` CLI hit the same store. Scale
your use of brains to the task — under-coordinating drifts, over-coordinating wastes
tokens.

**Quick lookup** ("where is X", "how does Y work"): skip the session ceremony.
Use `brains_knowledge_search` for recorded knowledge or `brains_search_repo` for
substring/symbol lookup. Empty results mean no match; neither path requires an
embedding model or graph index.

**Non-trivial, multi-step, or COLLABORATIVE work** (editing a shared repo, work others
may touch, a multi-session effort): coordinate, in order —
1. `brains_start_session`; read the `welcome` (who's active, recorded caveats).
2. `brains_knowledge_search` before re-deriving anything someone may have recorded.
3. `brains_claim_workspace` before you edit; record only NEW, reusable findings with
   `brains_knowledge_add` (don't re-record knowledge you just read); run the project's
   test/lint gate, then leave a `brains_set_handoff` when you stop.
4. Append a ledger event for meaningful work; file an ASK for a human decision and
   keep working — never block.

Rules of thumb: **retrieve, don't grep; reuse recorded knowledge, don't re-derive or
re-record it; validate before you hand off; coordinate only when others are involved.**
Keep tool calls and outputs lean — fewer round-trips and shorter messages cost less. If a
retrieval call reports that the workspace is unavailable, use the supported
substring/symbol lookup or report that state instead of silently re-searching. If MCP is unreachable, use
the `brains-ai` CLI — same store.
"""


# --- resolved context -----------------------------------------------------


@dataclass
class WireContext:
    """Everything the adapters need to render a brains entry.

    ``transport`` is ``"streamable-http"`` (default), explicit legacy
    ``"sse"``, or ``"stdio"``. HTTP clients point at ``url``. Codex reads its
    bearer token from ``bearer_token_env_var``; the token value is never
    rendered into its TOML. For stdio each tool spawns
    ``python -m brains.mcp.server --mode stdio`` with ``db_url`` in its env so
    every spawned server shares the one global brains DB.
    """

    transport: str = MCP_MODE_STREAMABLE_HTTP
    url: str = field(default_factory=mcp_http_url)
    api_key: str = ""
    bearer_token_env_var: str = MCP_CLIENT_BEARER_ENV
    python: str = "python"
    # Default to the absolute per-machine brain so a bare ``WireContext()``
    # can never embed a CWD-relative ``sqlite:///brains.db`` into a tool's
    # MCP config (which would fragment state into a per-workspace DB). The
    # real CLI caller passes ``_canonical_db_url()`` explicitly; this factory
    # is defence in depth for any other constructor.
    db_url: str = field(default_factory=_canonical_default_db_url)

    def stdio_args(self) -> list[str]:
        return ["-m", "brains.mcp.server", "--mode", "stdio"]

    def stdio_env(self) -> dict[str, str]:
        return {"BRAINS_DB_URL": self.db_url}

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# --- per-tool MCP entry builders -----------------------------------------
#
# Each returns the JSON-serialisable dict (JSON tools) or a TOML block
# string (Codex) for the brains server entry under the given transport.


def _copilot_entry(ctx: WireContext) -> dict[str, Any]:
    # Copilot CLI infers a local (stdio) server from `command`; SSE servers
    # carry an explicit `type` and `url`.
    if ctx.transport == "stdio":
        return {
            "command": ctx.python,
            "args": ctx.stdio_args(),
            "env": ctx.stdio_env(),
        }
    remote_type = "sse" if ctx.transport == MCP_MODE_SSE else "http"
    return {"type": remote_type, "url": ctx.url, "headers": ctx.auth_headers()}


def _claude_entry(ctx: WireContext) -> dict[str, Any]:
    # Claude Code always carries an explicit `type`.
    if ctx.transport == "stdio":
        return {
            "type": "stdio",
            "command": ctx.python,
            "args": ctx.stdio_args(),
            "env": ctx.stdio_env(),
        }
    remote_type = "sse" if ctx.transport == MCP_MODE_SSE else "http"
    return {"type": remote_type, "url": ctx.url, "headers": ctx.auth_headers()}


def _opencode_entry(ctx: WireContext) -> dict[str, Any]:
    if ctx.transport == "stdio":
        return {
            "type": "local",
            "command": [ctx.python, *ctx.stdio_args()],
            "environment": ctx.stdio_env(),
            "enabled": True,
        }
    return {
        "type": "remote",
        "url": ctx.url,
        "headers": ctx.auth_headers(),
        "oauth": False,
        "enabled": True,
    }


def _codex_block(ctx: WireContext) -> str:
    # Codex is TOML. stdio uses command/args + a nested env table; remote
    # uses the current Streamable HTTP URL + an environment-variable name.
    if ctx.transport == "stdio":
        args = ", ".join(json.dumps(a) for a in ctx.stdio_args())
        lines = [
            "[mcp_servers.brains]",
            f"command = {json.dumps(ctx.python)}",
            f"args = [{args}]",
            "",
            "[mcp_servers.brains.env]",
            f"BRAINS_DB_URL = {json.dumps(ctx.db_url)}",
        ]
        return "\n".join(lines)
    lines = [
        "[mcp_servers.brains]",
        f"url = {json.dumps(ctx.url)}",
    ]
    if ctx.bearer_token_env_var:
        lines.append(f"bearer_token_env_var = {json.dumps(ctx.bearer_token_env_var)}")
    return "\n".join(lines)


# --- adapter registry -----------------------------------------------------


@dataclass
class ToolAdapter:
    name: str
    display: str
    mcp_format: str  # "json" | "toml"
    _mcp_path: Callable[[Path], Path]
    _instr_path: Callable[[Path], Path]
    _detect: Callable[[Path], bool]
    _json_entry: Callable[[WireContext], dict[str, Any]] | None = None
    _toml_block: Callable[[WireContext], str] | None = None
    json_servers_key: str = "mcpServers"
    supports_sse: bool = True
    sse_experimental: bool = False
    wakeup_mode: str | None = None

    def mcp_path(self, home: Path) -> Path:
        return self._mcp_path(home)

    def instr_path(self, home: Path) -> Path:
        return self._instr_path(home)

    def detect(self, home: Path) -> bool:
        return self._detect(home)


ADAPTERS: dict[str, ToolAdapter] = {
    "copilot-cli": ToolAdapter(
        name="copilot-cli",
        display="GitHub Copilot CLI",
        mcp_format="json",
        _mcp_path=lambda h: h / ".copilot" / "mcp-config.json",
        _instr_path=lambda h: h / ".copilot" / "copilot-instructions.md",
        _detect=lambda h: (h / ".copilot").is_dir(),
        _json_entry=_copilot_entry,
        # The hook schema is retained for compatibility tests, but wiring stays
        # pull-only until a pinned real Copilot binary proves discovery and
        # continuation from the generated file.
        wakeup_mode=None,
    ),
    "claude-code": ToolAdapter(
        name="claude-code",
        display="Claude Code",
        mcp_format="json",
        _mcp_path=lambda h: h / ".claude.json",
        _instr_path=lambda h: h / ".claude" / "CLAUDE.md",
        _detect=lambda h: (h / ".claude").is_dir() or (h / ".claude.json").exists(),
        _json_entry=_claude_entry,
        wakeup_mode="turn_boundary",
    ),
    "codex": ToolAdapter(
        name="codex",
        display="OpenAI Codex CLI",
        mcp_format="toml",
        _mcp_path=lambda h: h / ".codex" / "config.toml",
        _instr_path=lambda h: h / ".codex" / "AGENTS.md",
        _detect=lambda h: (h / ".codex").is_dir(),
        _toml_block=_codex_block,
        supports_sse=False,
    ),
    "opencode": ToolAdapter(
        name="opencode",
        display="OpenCode",
        mcp_format="json",
        _mcp_path=lambda h: h / ".config" / "opencode" / "opencode.json",
        _instr_path=lambda h: h / ".config" / "opencode" / "AGENTS.md",
        _detect=lambda h: (h / ".config" / "opencode").is_dir(),
        _json_entry=_opencode_entry,
        json_servers_key="mcp",
    ),
}


def known_tools() -> list[str]:
    return list(ADAPTERS.keys())


# --- low-level file helpers ----------------------------------------------


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _harden(path: Path) -> None:
    """Best-effort tighten a secret-bearing file to owner-only (POSIX)."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _backup(path: Path, *, secure: bool = False) -> str | None:
    """Copy ``path`` to ``path.bak-<ts>`` if it exists. Returns backup name."""
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.bak-{_timestamp()}")
    backup.write_bytes(path.read_bytes())
    if secure:
        _harden(backup)
    return backup.name


def _write(path: Path, text: str, *, secure: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if secure:
        _harden(path)


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _digest(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _atomic_bytes(path: Path, content: bytes, *, secure: bool = False) -> None:
    """Replace *path* atomically with bytes staged in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if secure:
            _harden(tmp)
        os.replace(tmp, path)
        if secure:
            _harden(path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _cas_bytes(
    path: Path,
    expected: bytes | None,
    content: bytes | None,
    *,
    secure: bool = False,
) -> bool:
    """Mutate only when a last-moment byte-for-byte re-read matches *expected*."""
    if _snapshot(path) != expected:
        return False
    if content is None:
        if path.exists():
            path.unlink()
        return True
    _atomic_bytes(path, content, secure=secure)
    return True


@contextmanager
def _path_lock(path: Path, *, timeout: float = 2.0):
    """Fail-closed cross-process lock for one shared harness settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("settings-busy") from None
            time.sleep(0.025)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def _replace_sentinel_block(text: str, start: str, end: str, block: str) -> str:
    """Return ``text`` with the region between sentinels replaced by ``block``.

    If no sentinel region exists, the block (sentinel-wrapped) is appended.
    """
    wrapped = f"{start}\n{block}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: wrapped, text)
    if text == "":
        return wrapped + "\n"
    sep = "\n" if text.endswith("\n") else "\n\n"
    return f"{text}{sep}{wrapped}\n"


def _strip_sentinel_block(text: str, start: str, end: str) -> str:
    pattern = re.compile(
        r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("\n", text).lstrip("\n")


_COPILOT_WAKEUP_COMMAND = "brains-ai mailbox harness-wakeup --adapter copilot-cli"
_CLAUDE_WAKEUP_COMMAND = "brains-ai mailbox harness-wakeup --adapter claude-code"


def _copilot_wakeup_path(home: Path) -> Path:
    return home / ".copilot" / "hooks" / "brains.json"


def _claude_wakeup_path(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _claude_wakeup_state_paths(home: Path) -> tuple[Path, Path, Path]:
    state = home / ".claude" / ".brains-wakeup"
    return state / "settings.lock", state / "manifest.json", state / "prior-settings.bin"


def _read_wakeup_manifest(path: Path) -> dict[str, Any] | None:
    data, invalid = _read_json(path)
    required = {"version", "target", "prior_exists", "prior_sha256", "installed_sha256"}
    if invalid or set(data) != required or data.get("version") != 1:
        return None
    if data.get("target") != "settings.json" or not isinstance(data.get("prior_exists"), bool):
        return None
    if not isinstance(data.get("installed_sha256"), str):
        return None
    prior_sha = data.get("prior_sha256")
    if prior_sha is not None and not isinstance(prior_sha, str):
        return None
    return data


def _copilot_wakeup_document() -> dict[str, Any]:
    return {
        "version": 1,
        "hooks": {
            "agentStop": [
                {
                    "type": "command",
                    "bash": _COPILOT_WAKEUP_COMMAND,
                    "powershell": _COPILOT_WAKEUP_COMMAND,
                    "timeoutSec": 10,
                }
            ]
        },
    }


def _claude_wakeup_entry() -> dict[str, Any]:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": _CLAUDE_WAKEUP_COMMAND,
                "timeout": 10,
            }
        ],
    }


def _wakeup_status(adapter: ToolAdapter, home: Path) -> dict[str, Any]:
    if adapter.wakeup_mode is None:
        return {"installed": False, "mode": "pull", "reason": "adapter-unavailable"}
    if adapter.name == "copilot-cli":
        path = _copilot_wakeup_path(home)
        data, parse_error = _read_json(path)
        installed = not parse_error and data == _copilot_wakeup_document()
        reason = (
            "installed"
            if installed
            else "managed-path-conflict"
            if path.exists()
            else "consent-required"
        )
    else:
        path = _claude_wakeup_path(home)
        _lock, manifest_path, _backup_path = _claude_wakeup_state_paths(home)
        data, parse_error = _read_json(path)
        hooks = data.get("hooks") if not parse_error else None
        entries = hooks.get("Stop") if isinstance(hooks, dict) else None
        manifest = _read_wakeup_manifest(manifest_path) if manifest_path.exists() else None
        installed = (
            isinstance(entries, list)
            and _claude_wakeup_entry() in entries
            and manifest is not None
            and manifest["installed_sha256"] == _digest(_snapshot(path))
        )
        if installed:
            reason = "installed"
        elif parse_error:
            reason = "settings-invalid"
        elif hooks is not None and not isinstance(hooks, dict):
            reason = "hooks-invalid"
        elif entries is not None and not isinstance(entries, list):
            reason = "stop-hooks-invalid"
        elif isinstance(entries, list) and _claude_wakeup_entry() in entries:
            reason = "ownership-missing"
        else:
            reason = "consent-required"
    return {
        "installed": installed,
        "mode": adapter.wakeup_mode if installed else "pull",
        "reason": reason,
    }


def _wire_wakeup(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    if adapter.wakeup_mode is None:
        return {"action": "unavailable", "mode": "pull", "reason": "adapter-unavailable"}
    if adapter.name == "copilot-cli":
        path = _copilot_wakeup_path(home)
        expected = _copilot_wakeup_document()
        current, parse_error = _read_json(path)
        if path.exists() and (parse_error or current != expected):
            return {"action": "conflict", "mode": "pull", "reason": "managed-path-conflict"}
        action = "unchanged" if current == expected else "create"
        if dry_run and action == "create":
            return {"action": action, "mode": "pull", "planned_mode": adapter.wakeup_mode}
        if action == "create":
            _write(path, json.dumps(expected, indent=2) + "\n")
        return {"action": action, "mode": adapter.wakeup_mode}

    path = _claude_wakeup_path(home)
    lock_path, manifest_path, backup_path = _claude_wakeup_state_paths(home)

    def prepare() -> tuple[dict[str, Any] | None, bytes | None, str | None]:
        before = _snapshot(path)
        data, parse_error = _read_json(path)
        if parse_error:
            return None, before, "settings-invalid"
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            return None, before, "hooks-invalid"
        entries = hooks.setdefault("Stop", [])
        if not isinstance(entries, list):
            return None, before, "stop-hooks-invalid"
        entries.append(_claude_wakeup_entry())
        return data, before, None

    if dry_run:
        data, before, error = prepare()
        if error:
            return {"action": "conflict", "mode": "pull", "reason": error}
        return {
            "action": "update" if before is not None else "create",
            "mode": "pull",
            "planned_mode": adapter.wakeup_mode,
        }
    try:
        with _path_lock(lock_path):
            current_bytes = _snapshot(path)
            existing_manifest = (
                _read_wakeup_manifest(manifest_path) if manifest_path.exists() else None
            )
            if existing_manifest is not None:
                if existing_manifest["installed_sha256"] == _digest(current_bytes):
                    return {"action": "unchanged", "mode": adapter.wakeup_mode}
                return {"action": "conflict", "mode": "pull", "reason": "settings-changed"}
            current_data, before, error = prepare()
            if error:
                return {"action": "conflict", "mode": "pull", "reason": error}
            assert current_data is not None
            entries = cast("dict[str, Any]", current_data["hooks"])["Stop"]
            if entries.count(_claude_wakeup_entry()) > 1:
                return {"action": "conflict", "mode": "pull", "reason": "ownership-missing"}
            installed = (json.dumps(current_data, indent=2) + "\n").encode()
            manifest = {
                "version": 1,
                "target": "settings.json",
                "prior_exists": before is not None,
                "prior_sha256": _digest(before),
                "installed_sha256": _digest(installed),
            }
            if before is not None:
                _atomic_bytes(backup_path, before, secure=True)
            _atomic_bytes(
                manifest_path,
                (json.dumps(manifest, indent=2) + "\n").encode(),
                secure=True,
            )
            if not _cas_bytes(path, before, installed, secure=True):
                with contextlib.suppress(OSError):
                    manifest_path.unlink()
                with contextlib.suppress(OSError):
                    backup_path.unlink()
                return {"action": "conflict", "mode": "pull", "reason": "settings-changed"}
            return {
                "action": "update" if before is not None else "create",
                "mode": adapter.wakeup_mode,
            }
    except TimeoutError:
        return {"action": "conflict", "mode": "pull", "reason": "settings-busy"}


def _unwire_wakeup(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    if adapter.wakeup_mode is None:
        return {"action": "absent", "mode": "pull"}
    if adapter.name == "copilot-cli":
        path = _copilot_wakeup_path(home)
        data, parse_error = _read_json(path)
        if not path.exists():
            return {"action": "absent", "mode": "pull"}
        if parse_error or data != _copilot_wakeup_document():
            return {"action": "conflict", "mode": "pull", "reason": "managed-path-conflict"}
        if not dry_run:
            path.unlink()
        return {"action": "remove", "mode": "pull"}

    path = _claude_wakeup_path(home)
    lock_path, manifest_path, backup_path = _claude_wakeup_state_paths(home)
    manifest = _read_wakeup_manifest(manifest_path) if manifest_path.exists() else None
    if manifest is None:
        return {"action": "absent", "mode": "pull"}
    if dry_run:
        return {"action": "remove", "mode": "pull"}
    try:
        with _path_lock(lock_path):
            manifest = _read_wakeup_manifest(manifest_path)
            current = _snapshot(path)
            if manifest is None or manifest["installed_sha256"] != _digest(current):
                return {"action": "conflict", "mode": "pull", "reason": "settings-changed"}
            prior: bytes | None = None
            if manifest["prior_exists"]:
                prior = _snapshot(backup_path)
                if prior is None or manifest["prior_sha256"] != _digest(prior):
                    return {"action": "conflict", "mode": "pull", "reason": "backup-invalid"}
            if not _cas_bytes(path, current, prior, secure=True):
                return {"action": "conflict", "mode": "pull", "reason": "settings-changed"}
            with contextlib.suppress(OSError):
                backup_path.unlink()
            with contextlib.suppress(OSError):
                manifest_path.unlink()
            return {"action": "remove", "mode": "pull"}
    except TimeoutError:
        return {"action": "conflict", "mode": "pull", "reason": "settings-busy"}


# --- JSON mcp wiring (Copilot, Claude) -----------------------------------


def _read_json(path: Path) -> tuple[dict[str, Any], bool]:
    """Read a JSON object. Returns ``(data, parse_error)``.

    ``parse_error`` is True only when a NON-EMPTY file fails to parse or is
    not a JSON object. Callers must refuse to rewrite in that case so they
    never truncate a user's real config (e.g. ``~/.claude.json``) on a torn
    or concurrent read. Absent / empty files are not errors.
    """
    if not path.exists():
        return {}, False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}, True
    if raw.strip() == "":
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def _load_json(path: Path) -> dict[str, Any]:
    """Read-only convenience for status(): empty dict on any problem."""
    data, _ = _read_json(path)
    return data


def _wire_json(adapter: ToolAdapter, home: Path, ctx: WireContext, dry_run: bool) -> dict[str, Any]:
    path = adapter.mcp_path(home)
    data, parse_error = _read_json(path)
    result: dict[str, Any] = {"path": str(path), "transport": ctx.transport}
    if parse_error:
        result["action"] = "error"
        result["detail"] = "existing config is not valid JSON; left untouched"
        return result
    assert adapter._json_entry is not None

    servers = data.get(adapter.json_servers_key)
    if not isinstance(servers, dict):
        servers = {}
    existing = servers.get(SERVER_KEY)
    if isinstance(existing, dict) and existing.get("_brains_managed") is not True:
        result["action"] = "conflict"
        result["detail"] = (
            "an unmanaged 'brains' MCP entry already exists; remove it or run `brains-ai unwire` first"
        )
        return result

    entry = dict(adapter._json_entry(ctx))
    entry["_brains_managed"] = True
    action = "update" if isinstance(existing, dict) else "create"
    servers[SERVER_KEY] = entry
    data[adapter.json_servers_key] = servers

    result["action"] = action
    if dry_run:
        result["preview"] = entry
        return result
    secure = ctx.transport != MCP_MODE_STDIO and bool(ctx.api_key)
    result["backup"] = _backup(path, secure=secure)
    _write(path, json.dumps(data, indent=2) + "\n", secure=secure)
    return result


def _unwire_json(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    path = adapter.mcp_path(home)
    data, parse_error = _read_json(path)
    result: dict[str, Any] = {"path": str(path)}
    if parse_error:
        result["action"] = "error"
        result["detail"] = "existing config is not valid JSON; left untouched"
        return result
    servers = data.get(adapter.json_servers_key)
    existing = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    if not isinstance(existing, dict):
        result["action"] = "absent"
        return result
    if existing.get("_brains_managed") is not True:
        result["action"] = "skipped"
        result["detail"] = "existing 'brains' entry is not managed by brains wire; left untouched"
        return result
    result["action"] = "remove"
    if dry_run:
        return result
    result["backup"] = _backup(path)
    managed_servers = cast("dict[str, Any]", servers)
    del managed_servers[SERVER_KEY]
    data[adapter.json_servers_key] = managed_servers
    _write(path, json.dumps(data, indent=2) + "\n")
    return result


# --- TOML mcp wiring (Codex) ---------------------------------------------


def _toml_has_unmanaged_entry(text: str) -> bool:
    """True if a [mcp_servers.brains] table exists OUTSIDE our sentinels."""
    outside = _strip_sentinel_block(text, TOML_START, TOML_END)
    return re.search(r"(?m)^\s*\[mcp_servers\.brains(?:\.[a-z_]+)?\]", outside) is not None


def _wire_toml(adapter: ToolAdapter, home: Path, ctx: WireContext, dry_run: bool) -> dict[str, Any]:
    path = adapter.mcp_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    assert adapter._toml_block is not None
    block = adapter._toml_block(ctx)
    result: dict[str, Any] = {"path": str(path), "transport": ctx.transport}
    if ctx.transport != MCP_MODE_STDIO:
        result["url"] = ctx.url
        result["bearer_token_env_var"] = ctx.bearer_token_env_var or None
        result["bearer_token_env_available"] = _bearer_env_state(ctx) == "available"

    if _toml_has_unmanaged_entry(text):
        result["action"] = "conflict"
        result["detail"] = (
            "an unmanaged [mcp_servers.brains] table already exists; "
            "remove it or run `brains-ai unwire` first"
        )
        return result
    if ctx.transport == MCP_MODE_SSE and not adapter.supports_sse:
        result["action"] = "error"
        result["detail"] = (
            f"{adapter.display} does not support the legacy Brains SSE endpoint; "
            "use streamable-http or stdio"
        )
        return result

    has_block = TOML_START in text
    result["action"] = "update" if has_block else "create"
    if dry_run:
        result["preview"] = block
        return result
    secure = ctx.transport != MCP_MODE_STDIO and bool(ctx.api_key)
    result["backup"] = _backup(path, secure=secure)
    _write(
        path,
        _replace_sentinel_block(text, TOML_START, TOML_END, block),
        secure=secure,
    )
    return result


def _unwire_toml(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    path = adapter.mcp_path(home)
    result: dict[str, Any] = {"path": str(path)}
    if not path.exists() or TOML_START not in path.read_text(encoding="utf-8"):
        result["action"] = "absent"
        return result
    result["action"] = "remove"
    if dry_run:
        return result
    text = path.read_text(encoding="utf-8")
    result["backup"] = _backup(path)
    _write(path, _strip_sentinel_block(text, TOML_START, TOML_END))
    return result


# --- instruction (rule) injection ----------------------------------------


def _wire_rule(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    path = adapter.instr_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    action = "update" if MD_START in text else ("append" if text else "create")
    result: dict[str, Any] = {"path": str(path), "action": action}
    if dry_run:
        return result
    result["backup"] = _backup(path)
    _write(path, _replace_sentinel_block(text, MD_START, MD_END, RULE_BODY.rstrip("\n")))
    return result


def _unwire_rule(adapter: ToolAdapter, home: Path, dry_run: bool) -> dict[str, Any]:
    path = adapter.instr_path(home)
    result: dict[str, Any] = {"path": str(path)}
    if not path.exists() or MD_START not in path.read_text(encoding="utf-8"):
        result["action"] = "absent"
        return result
    result["action"] = "remove"
    if dry_run:
        return result
    text = path.read_text(encoding="utf-8")
    result["backup"] = _backup(path)
    _write(path, _strip_sentinel_block(text, MD_START, MD_END))
    return result


# --- public API -----------------------------------------------------------


def _select_adapters(tools: list[str] | None, home: Path, force: bool) -> list[ToolAdapter]:
    if tools:
        return [ADAPTERS[t] for t in tools if t in ADAPTERS]
    if force:
        return list(ADAPTERS.values())
    return [a for a in ADAPTERS.values() if a.detect(home)]


def _bearer_env_state(ctx: WireContext) -> str:
    """Validate the named client credential without exposing either value."""

    if not ctx.bearer_token_env_var:
        return "missing"
    client_token = os.environ.get(ctx.bearer_token_env_var)
    if not client_token:
        return "missing"
    if not ctx.api_key:
        return "effective-key-unavailable"
    return (
        "available"
        if hmac.compare_digest(client_token.encode("utf-8"), ctx.api_key.encode("utf-8"))
        else "mismatch"
    )


def wire(
    home: Path,
    ctx: WireContext,
    *,
    tools: list[str] | None = None,
    rules: bool = True,
    mailbox_wakeups: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Wire brains into the selected (or all detected) tools.

    Returns a JSON-serialisable report with a per-tool ``mcp`` and ``rule``
    result. ``force`` wires even tools whose config dir is absent.
    """
    adapters = _select_adapters(tools, home, force)
    report: dict[str, Any] = {
        "transport": ctx.transport,
        "url": ctx.url if ctx.transport != MCP_MODE_STDIO else None,
        "dry_run": dry_run,
        "tools": [],
    }
    codex = next((adapter for adapter in adapters if adapter.name == "codex"), None)
    bearer_state = _bearer_env_state(ctx)
    if codex is not None and ctx.transport != MCP_MODE_STDIO and bearer_state != "available":
        env_name = ctx.bearer_token_env_var or MCP_CLIENT_BEARER_ENV
        if bearer_state == "mismatch":
            remediation = (
                f"{env_name} does not match the effective Brains API credential; "
                "it must already match in the environment that launches Codex. "
                "Brains cannot change that parent environment; then rerun "
                "`brains-ai wire`"
            )
        elif bearer_state == "effective-key-unavailable":
            remediation = (
                "the effective Brains API credential is unavailable for validation; "
                f"initialize it first, then make {env_name} match it securely in the "
                "environment that launches Codex. Brains cannot change that parent "
                "environment; then rerun `brains-ai wire`"
            )
        else:
            remediation = (
                f"{env_name} is unavailable to this process; it must already match the "
                "effective Brains API credential in the environment that launches Codex. "
                "Brains cannot change that parent environment; then rerun `brains-ai wire`"
            )
        report["ok"] = False
        report["tools"].append(
            {
                "tool": codex.name,
                "display": codex.display,
                "detected": codex.detect(home),
                "mcp": {
                    "path": str(codex.mcp_path(home)),
                    "transport": ctx.transport,
                    "url": ctx.url,
                    "action": "error",
                    "bearer_token_env_var": env_name,
                    "bearer_token_env_available": False,
                    "detail": remediation,
                },
                "mailbox_notification_mode": "pull",
                "mailbox_wakeup": {
                    "action": "skipped",
                    "mode": "pull",
                    "reason": "mcp-wiring-failed",
                },
            }
        )
        return report
    if adapters and ctx.transport != MCP_MODE_STDIO and not ctx.api_key:
        adapter = adapters[0]
        report["ok"] = False
        report["tools"].append(
            {
                "tool": adapter.name,
                "display": adapter.display,
                "detected": adapter.detect(home),
                "mcp": {
                    "path": str(adapter.mcp_path(home)),
                    "transport": ctx.transport,
                    "url": ctx.url,
                    "action": "error",
                    "detail": (
                        "the effective Brains API credential is unavailable; run "
                        "`brains-ai init` or `brains-ai setup` before remote wiring"
                    ),
                },
                "mailbox_notification_mode": "pull",
                "mailbox_wakeup": {
                    "action": "skipped",
                    "mode": "pull",
                    "reason": "mcp-wiring-failed",
                },
            }
        )
        return report
    for adapter in adapters:
        detected = adapter.detect(home)
        warnings: list[str] = []
        if ctx.transport == MCP_MODE_SSE and adapter.sse_experimental:
            warnings.append("SSE/remote MCP is experimental for this tool")
        if adapter.mcp_format == "json":
            mcp = _wire_json(adapter, home, ctx, dry_run)
        else:
            mcp = _wire_toml(adapter, home, ctx, dry_run)
        entry: dict[str, Any] = {
            "tool": adapter.name,
            "display": adapter.display,
            "detected": detected,
            "mcp": mcp,
        }
        if not mailbox_wakeups:
            wakeup = {"action": "skipped", "mode": "pull", "reason": "consent-required"}
        elif mcp.get("action") in {"error", "conflict"}:
            wakeup = {"action": "skipped", "mode": "pull", "reason": "mcp-wiring-failed"}
        else:
            wakeup = _wire_wakeup(adapter, home, dry_run)
        entry["mailbox_wakeup"] = wakeup
        entry["mailbox_notification_mode"] = wakeup["mode"]
        if rules:
            entry["rule"] = _wire_rule(adapter, home, dry_run)
        if warnings:
            entry["warnings"] = warnings
        report["tools"].append(entry)
    report["ok"] = all(
        entry["mcp"].get("action") not in {"error", "conflict"}
        and entry["mailbox_wakeup"].get("action") not in {"error", "conflict"}
        for entry in report["tools"]
    )
    return report


def unwire(
    home: Path,
    *,
    tools: list[str] | None = None,
    rules: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove brains MCP entries and rule blocks from the selected tools."""
    adapters = [ADAPTERS[t] for t in tools if t in ADAPTERS] if tools else list(ADAPTERS.values())
    report: dict[str, Any] = {"dry_run": dry_run, "tools": []}
    for adapter in adapters:
        if adapter.mcp_format == "json":
            mcp = _unwire_json(adapter, home, dry_run)
        else:
            mcp = _unwire_toml(adapter, home, dry_run)
        entry: dict[str, Any] = {
            "tool": adapter.name,
            "mcp": mcp,
            "mailbox_wakeup": _unwire_wakeup(adapter, home, dry_run),
        }
        if rules:
            entry["rule"] = _unwire_rule(adapter, home, dry_run)
        report["tools"].append(entry)
    return report


def status(home: Path) -> dict[str, Any]:
    """Report, per tool, whether it is detected and currently wired."""
    out: list[dict[str, Any]] = []
    for adapter in ADAPTERS.values():
        mcp_path = adapter.mcp_path(home)
        instr_path = adapter.instr_path(home)
        wired_mcp = False
        transport = None
        url = None
        bearer_token_env_var = None
        if adapter.mcp_format == "json":
            data = _load_json(mcp_path)
            servers = data.get(adapter.json_servers_key)
            if isinstance(servers, dict) and isinstance(servers.get(SERVER_KEY), dict):
                wired_mcp = True
                server = servers[SERVER_KEY]
                if server.get("command") or server.get("type") == "local":
                    transport = MCP_MODE_STDIO
                elif server.get("type") == "sse" or str(server.get("url", "")).endswith("/sse"):
                    transport = MCP_MODE_SSE
                else:
                    transport = MCP_MODE_STREAMABLE_HTTP
                url = server.get("url")
        elif mcp_path.exists() and TOML_START in mcp_path.read_text(encoding="utf-8"):
            wired_mcp = True
            managed = (
                mcp_path.read_text(encoding="utf-8").split(TOML_START, 1)[-1].split(TOML_END, 1)[0]
            )
            if "command =" in managed:
                transport = MCP_MODE_STDIO
            else:
                url_match = re.search(r'(?m)^\s*url\s*=\s*["\']([^"\']+)["\']', managed)
                url = url_match.group(1) if url_match else None
                transport = (
                    MCP_MODE_SSE
                    if isinstance(url, str) and url.endswith("/sse")
                    else MCP_MODE_STREAMABLE_HTTP
                )
                env_match = re.search(
                    r'(?m)^\s*bearer_token_env_var\s*=\s*["\']([^"\']+)["\']',
                    managed,
                )
                bearer_token_env_var = env_match.group(1) if env_match else None
        wired_rule = instr_path.exists() and MD_START in instr_path.read_text(encoding="utf-8")
        wakeup = _wakeup_status(adapter, home)
        out.append(
            {
                "tool": adapter.name,
                "display": adapter.display,
                "detected": adapter.detect(home),
                "mcp_path": str(mcp_path),
                "mcp_wired": wired_mcp,
                "mcp_transport": transport,
                "mcp_url": url,
                "bearer_token_env_var": bearer_token_env_var,
                "bearer_token_env_available": (
                    bool(os.environ.get(bearer_token_env_var)) if bearer_token_env_var else None
                ),
                "instr_path": str(instr_path),
                "rule_wired": bool(wired_rule),
                "mailbox_notification_mode": wakeup["mode"],
                "mailbox_wakeup": wakeup,
            }
        )
    return {"tools": out}
