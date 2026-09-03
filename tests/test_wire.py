"""Tests for ``brains wire`` — the agentic-tool auto-discovery layer.

Everything is exercised against a synthetic ``home`` (tmp_path) with the
four tier-1 tool dirs present, mimicking a clean-slate machine — the same
shape the Docker sandbox uses — so no real config is ever touched.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from brains import wire


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Clean-slate: tool config dirs exist but hold no brains entry yet.
    (tmp_path / ".copilot").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", "TESTKEY")
    return tmp_path


@pytest.fixture(autouse=True)
def compatible_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wire,
        "_opencode_compatibility",
        lambda: (wire.OPENCODE_SUPPORTED_VERSION, "compatible"),
    )


def _sse_ctx() -> wire.WireContext:
    return wire.WireContext(transport="sse", url="http://127.0.0.1:9877/sse", api_key="TESTKEY")


def _http_ctx() -> wire.WireContext:
    return wire.WireContext(
        transport="streamable-http",
        url="http://127.0.0.1:9877/mcp",
        api_key="TESTKEY",
    )


def _stdio_ctx() -> wire.WireContext:
    return wire.WireContext(
        transport="stdio",
        python="/usr/bin/python3",
        db_url="sqlite:////root/.brains/brains.db",
    )


def test_wirecontext_default_db_url_is_canonical_not_cwd_relative() -> None:
    """A bare ``WireContext()`` must never carry the CWD-relative sentinel.

    The historical default ``sqlite:///brains.db`` is resolved relative to
    the current working directory, so embedding it into a tool's stdio MCP
    env would fragment the shared brain into a per-workspace DB. The default
    must resolve to the absolute per-machine path instead.
    """
    from brains.config import _canonical_default_db_url

    ctx = wire.WireContext()
    assert ctx.db_url != "sqlite:///brains.db"
    assert ctx.db_url == _canonical_default_db_url()
    assert ctx.db_url.startswith("sqlite:///")
    # stdio env must propagate the canonical (absolute) URL, never the bare one
    assert ctx.stdio_env()["BRAINS_DB_URL"] == _canonical_default_db_url()


# --- detection ------------------------------------------------------------


def test_detects_all_four_on_clean_slate(home: Path) -> None:
    report = wire.status(home)
    by = {t["tool"]: t for t in report["tools"]}
    assert by["copilot-cli"]["detected"] is True
    assert by["claude-code"]["detected"] is True
    assert by["codex"]["detected"] is True
    assert by["opencode"]["detected"] is True
    # Nothing wired yet.
    assert all(not t["mcp_wired"] and not t["rule_wired"] for t in report["tools"])
    assert {row["tool"]: row["mailbox_notification_mode"] for row in report["tools"]} == {
        "copilot-cli": "pull",
        "claude-code": "pull",
        "codex": "pull",
        "opencode": "pull",
    }


def test_absent_tool_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()  # only codex present
    report = wire.wire(tmp_path, _sse_ctx())
    assert [t["tool"] for t in report["tools"]] == ["codex"]


# --- SSE schemas ----------------------------------------------------------


def test_copilot_sse_schema(home: Path) -> None:
    wire.wire(home, _sse_ctx(), rules=False)
    data = json.loads((home / ".copilot" / "mcp-config.json").read_text())
    entry = data["mcpServers"]["brains"]
    assert entry["type"] == "sse"
    assert entry["url"] == "http://127.0.0.1:9877/sse"
    assert entry["headers"]["Authorization"] == "Bearer TESTKEY"


def test_claude_sse_schema(home: Path) -> None:
    wire.wire(home, _sse_ctx(), rules=False)
    data = json.loads((home / ".claude.json").read_text())
    entry = data["mcpServers"]["brains"]
    assert entry["type"] == "sse"
    assert entry["url"].endswith("/sse")
    assert entry["headers"]["Authorization"] == "Bearer TESTKEY"


def test_codex_streamable_http_schema_references_token_env_only(home: Path) -> None:
    wire.wire(home, _http_ctx(), rules=False)
    text = (home / ".codex" / "config.toml").read_text()
    assert wire.TOML_START in text and wire.TOML_END in text
    assert "[mcp_servers.brains]" in text
    assert 'url = "http://127.0.0.1:9877/mcp"' in text
    assert 'bearer_token_env_var = "BRAINS_MCP_BEARER_TOKEN"' in text
    assert "experimental_use_rmcp_client" not in text
    assert "TESTKEY" not in text
    assert "bearer_token =" not in text


@pytest.mark.parametrize("client_value", [None, "", "WRONGKEY", "wrong-☃"])
def test_codex_remote_wiring_fails_closed_before_writing_when_token_invalid(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_value: str | None,
) -> None:
    if client_value is None:
        monkeypatch.delenv("BRAINS_MCP_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", client_value)

    report = wire.wire(home, _http_ctx(), rules=True)

    assert report["ok"] is False
    assert report["tools"][0]["tool"] == "codex"
    assert report["tools"][0]["mcp"]["action"] == "error"
    detail = report["tools"][0]["mcp"]["detail"]
    assert "BRAINS_MCP_BEARER_TOKEN" in detail
    assert "TESTKEY" not in detail
    assert "WRONGKEY" not in detail
    assert "wrong-☃" not in detail
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".codex" / "AGENTS.md").exists()
    assert not (home / ".copilot" / "mcp-config.json").exists()


def test_remote_wiring_fails_before_writes_when_effective_key_is_missing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", "synthetic-client-key")
    ctx = _http_ctx()
    ctx.api_key = ""

    report = wire.wire(home, ctx, rules=True)

    assert report["ok"] is False
    assert report["tools"][0]["mcp"]["action"] == "error"
    assert "effective Brains API credential" in report["tools"][0]["mcp"]["detail"]
    assert "synthetic-client-key" not in report["tools"][0]["mcp"]["detail"]
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".codex" / "AGENTS.md").exists()


def test_opencode_sse_schema(home: Path) -> None:
    wire.wire(home, _sse_ctx(), rules=False)
    data = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    entry = data["mcp"]["brains"]
    assert entry["type"] == "remote"
    assert entry["url"] == "http://127.0.0.1:9877/sse"
    assert entry["headers"]["Authorization"] == "Bearer TESTKEY"
    assert entry["oauth"] is False
    assert entry["enabled"] is True
    assert entry["_brains_managed"] is True


def test_codex_rejects_explicit_legacy_sse(home: Path) -> None:
    report = wire.wire(home, _sse_ctx(), tools=["codex"])
    codex = report["tools"][0]
    assert codex["mcp"]["action"] == "error"
    assert "does not support" in codex["mcp"]["detail"]
    assert not (home / ".codex" / "config.toml").exists()


def test_streamable_http_schemas_for_current_remote_clients(home: Path) -> None:
    wire.wire(home, _http_ctx(), rules=False)
    copilot = json.loads((home / ".copilot" / "mcp-config.json").read_text())
    claude = json.loads((home / ".claude.json").read_text())
    opencode = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    assert copilot["mcpServers"]["brains"]["type"] == "http"
    assert copilot["mcpServers"]["brains"]["url"].endswith("/mcp")
    assert claude["mcpServers"]["brains"]["type"] == "http"
    assert claude["mcpServers"]["brains"]["url"].endswith("/mcp")
    assert opencode["mcp"]["brains"]["type"] == "remote"
    assert opencode["mcp"]["brains"]["url"].endswith("/mcp")


# --- stdio schemas --------------------------------------------------------


def test_copilot_stdio_schema(home: Path) -> None:
    wire.wire(home, _stdio_ctx(), rules=False)
    entry = json.loads((home / ".copilot" / "mcp-config.json").read_text())["mcpServers"]["brains"]
    assert entry["command"] == "/usr/bin/python3"
    assert entry["args"] == ["-m", "brains.mcp.server", "--mode", "stdio"]
    assert entry["env"]["BRAINS_DB_URL"] == "sqlite:////root/.brains/brains.db"
    assert "type" not in entry  # Copilot infers local from command


def test_claude_stdio_has_type(home: Path) -> None:
    wire.wire(home, _stdio_ctx(), rules=False)
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["brains"]
    assert entry["type"] == "stdio"
    assert entry["args"][-1] == "stdio"


def test_codex_stdio_schema(home: Path) -> None:
    wire.wire(home, _stdio_ctx(), rules=False)
    text = (home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.brains]" in text
    assert 'command = "/usr/bin/python3"' in text
    assert "[mcp_servers.brains.env]" in text
    assert 'BRAINS_DB_URL = "sqlite:////root/.brains/brains.db"' in text


def test_opencode_stdio_schema(home: Path) -> None:
    wire.wire(home, _stdio_ctx(), rules=False)
    data = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    entry = data["mcp"]["brains"]
    assert entry["type"] == "local"
    assert entry["command"] == [
        "/usr/bin/python3",
        "-m",
        "brains.mcp.server",
        "--mode",
        "stdio",
    ]
    assert entry["environment"]["BRAINS_DB_URL"] == "sqlite:////root/.brains/brains.db"
    assert entry["enabled"] is True


# --- rule injection -------------------------------------------------------


def test_rule_injected_into_instruction_files(home: Path) -> None:
    wire.wire(home, _sse_ctx())
    for rel in (
        ".copilot/copilot-instructions.md",
        ".claude/CLAUDE.md",
        ".codex/AGENTS.md",
        ".config/opencode/AGENTS.md",
    ):
        text = (home / rel).read_text()
        assert wire.MD_START in text and wire.MD_END in text
        assert "brains_start_session" in text
        assert "coordination plane" in text


def test_rule_preserves_existing_content(home: Path) -> None:
    instr = home / ".codex" / "AGENTS.md"
    instr.write_text("# My house rules\n\nKeep diffs small.\n", encoding="utf-8")
    wire.wire(home, _sse_ctx(), tools=["codex"])
    text = instr.read_text()
    assert "# My house rules" in text  # operator content survives
    assert wire.MD_START in text


# --- idempotency + backups ------------------------------------------------


def test_wire_is_idempotent(home: Path) -> None:
    wire.wire(home, _http_ctx())
    first = (home / ".copilot" / "mcp-config.json").read_text()
    report2 = wire.wire(home, _http_ctx())
    second = (home / ".copilot" / "mcp-config.json").read_text()
    assert first == second  # no drift on a second run
    # Second run reports an update, not a create.
    cop = next(t for t in report2["tools"] if t["tool"] == "copilot-cli")
    assert cop["mcp"]["action"] == "update"


def test_rewire_makes_backup(home: Path) -> None:
    wire.wire(home, _http_ctx())
    wire.wire(home, _stdio_ctx())  # switch transport -> backup of prior file
    backups = list((home / ".copilot").glob("mcp-config.json.bak-*"))
    assert backups, "expected a timestamped backup on re-wire"


def test_only_one_brains_server_after_repeated_wire(home: Path) -> None:
    for _ in range(3):
        wire.wire(home, _http_ctx())
    text = (home / ".codex" / "config.toml").read_text()
    assert text.count("[mcp_servers.brains]") == 1


def test_codex_migrates_only_managed_legacy_sse_block(home: Path) -> None:
    cfg = home / ".codex" / "config.toml"
    cfg.write_text(
        'model = "gpt-example"\n\n'
        f"{wire.TOML_START}\n"
        "[mcp_servers.brains]\n"
        'url = "http://127.0.0.1:9877/sse"\n'
        "experimental_use_rmcp_client = true\n"
        'bearer_token = "legacy-value"\n'
        f"{wire.TOML_END}\n",
        encoding="utf-8",
    )

    report = wire.wire(home, _http_ctx(), tools=["codex"], rules=False)

    assert report["tools"][0]["mcp"]["action"] == "update"
    text = cfg.read_text(encoding="utf-8")
    assert 'model = "gpt-example"' in text
    assert 'url = "http://127.0.0.1:9877/mcp"' in text
    assert 'bearer_token_env_var = "BRAINS_MCP_BEARER_TOKEN"' in text
    assert "/sse" not in text
    assert "legacy-value" not in text
    assert "experimental_use_rmcp_client" not in text


def test_status_reports_actual_url_transport_and_token_env_availability(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire.wire(home, _http_ctx(), tools=["codex"], rules=False)
    monkeypatch.delenv("BRAINS_MCP_BEARER_TOKEN", raising=False)

    codex = next(row for row in wire.status(home)["tools"] if row["tool"] == "codex")
    assert codex["mcp_transport"] == "streamable-http"
    assert codex["mcp_url"] == "http://127.0.0.1:9877/mcp"
    assert codex["bearer_token_env_var"] == "BRAINS_MCP_BEARER_TOKEN"
    assert codex["bearer_token_env_available"] is False

    monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", "synthetic-test-value")
    codex = next(row for row in wire.status(home)["tools"] if row["tool"] == "codex")
    assert codex["bearer_token_env_available"] is True


def test_status_reports_managed_legacy_sse_truthfully(home: Path) -> None:
    cfg = home / ".codex" / "config.toml"
    cfg.write_text(
        f"{wire.TOML_START}\n"
        "[mcp_servers.brains]\n"
        'url = "http://127.0.0.1:9877/sse"\n'
        f"{wire.TOML_END}\n",
        encoding="utf-8",
    )
    codex = next(row for row in wire.status(home)["tools"] if row["tool"] == "codex")
    assert codex["mcp_transport"] == "sse"
    assert codex["mcp_url"].endswith("/sse")


# --- conflict safety ------------------------------------------------------


def test_codex_unmanaged_entry_is_not_clobbered(home: Path) -> None:
    cfg = home / ".codex" / "config.toml"
    cfg.write_text('[mcp_servers.brains]\ncommand = "my-own-brains"\n', encoding="utf-8")
    report = wire.wire(home, _sse_ctx(), tools=["codex"])
    assert report["tools"][0]["mcp"]["action"] == "conflict"
    # The operator's own entry is untouched.
    assert 'command = "my-own-brains"' in cfg.read_text()


def test_copilot_unmanaged_entry_is_not_clobbered(home: Path) -> None:
    cfg = home / ".copilot" / "mcp-config.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"brains": {"command": "my-own-brains", "args": ["--mine"]}}}),
        encoding="utf-8",
    )
    report = wire.wire(home, _sse_ctx(), tools=["copilot-cli"])
    assert report["tools"][0]["mcp"]["action"] == "conflict"
    assert json.loads(cfg.read_text())["mcpServers"]["brains"]["command"] == "my-own-brains"


def test_claude_unmanaged_entry_preserves_other_keys(home: Path) -> None:
    cfg = home / ".claude.json"
    cfg.write_text(
        json.dumps({"userID": "abc", "mcpServers": {"brains": {"command": "mine"}}}),
        encoding="utf-8",
    )
    report = wire.wire(home, _sse_ctx(), tools=["claude-code"])
    assert report["tools"][0]["mcp"]["action"] == "conflict"
    data = json.loads(cfg.read_text())
    assert data["userID"] == "abc"
    assert data["mcpServers"]["brains"]["command"] == "mine"


def test_opencode_unmanaged_entry_is_not_clobbered(home: Path) -> None:
    cfg = home / ".config" / "opencode" / "opencode.json"
    cfg.write_text(
        json.dumps(
            {
                "theme": "system",
                "mcp": {"brains": {"type": "local", "command": ["mine"]}},
            }
        ),
        encoding="utf-8",
    )
    report = wire.wire(home, _sse_ctx(), tools=["opencode"])
    assert report["tools"][0]["mcp"]["action"] == "conflict"
    data = json.loads(cfg.read_text())
    assert data["theme"] == "system"
    assert data["mcp"]["brains"]["command"] == ["mine"]


def test_unwire_skips_unmanaged_json_entry(home: Path) -> None:
    cfg = home / ".copilot" / "mcp-config.json"
    cfg.write_text(json.dumps({"mcpServers": {"brains": {"command": "mine"}}}), encoding="utf-8")
    report = wire.unwire(home, tools=["copilot-cli"])
    assert report["tools"][0]["mcp"]["action"] == "skipped"
    assert "brains" in json.loads(cfg.read_text())["mcpServers"]


def test_unparseable_json_is_not_truncated(home: Path) -> None:
    cfg = home / ".claude.json"
    original = '{ "userID": "abc", this is not valid json '
    cfg.write_text(original, encoding="utf-8")
    report = wire.wire(home, _sse_ctx(), tools=["claude-code"])
    assert report["tools"][0]["mcp"]["action"] == "error"
    assert cfg.read_text() == original  # left byte-for-byte untouched


# --- secret hardening -----------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_sse_configs_are_owner_only(home: Path) -> None:
    wire.wire(home, _http_ctx(), rules=False)
    for rel in (
        ".copilot/mcp-config.json",
        ".claude.json",
        ".codex/config.toml",
        ".config/opencode/opencode.json",
    ):
        mode = stat.S_IMODE((home / rel).stat().st_mode)
        assert mode == 0o600, f"{rel} is {oct(mode)}, expected 0o600"


# --- unwire ---------------------------------------------------------------


def test_unwire_removes_everything(home: Path) -> None:
    wire.wire(home, _http_ctx())
    wire.unwire(home)
    status = wire.status(home)
    assert all(not t["mcp_wired"] and not t["rule_wired"] for t in status["tools"])
    # mcpServers key remains but without the brains entry.
    data = json.loads((home / ".claude.json").read_text())
    assert "brains" not in data.get("mcpServers", {})
    opencode = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    assert "brains" not in opencode.get("mcp", {})
    # Codex sentinels are gone.
    assert wire.TOML_START not in (home / ".codex" / "config.toml").read_text()


def test_unwire_preserves_other_servers(home: Path) -> None:
    cfg = home / ".copilot" / "mcp-config.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}),
        encoding="utf-8",
    )
    wire.wire(home, _sse_ctx(), tools=["copilot-cli"])
    wire.unwire(home, tools=["copilot-cli"])
    data = json.loads(cfg.read_text())
    assert "playwright" in data["mcpServers"]
    assert "brains" not in data["mcpServers"]


# --- dry run --------------------------------------------------------------


def test_dry_run_writes_nothing(home: Path) -> None:
    wire.wire(home, _http_ctx(), dry_run=True)
    assert not (home / ".copilot" / "mcp-config.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".config" / "opencode" / "opencode.json").exists()
