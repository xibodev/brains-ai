"""Tests for the ``brains-ai setup`` first-run bootstrapper.

``setup`` is a thin orchestrator over ``init``, ``wire``, and
``features --status`` — these tests verify the orchestration shape,
not the individual subcommands (those have their own tests).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brains.cli.app import app


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME + the brains state at a clean tmp dir so wire writes
    don't touch the real ~/.copilot etc., and the DB is sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Sandbox brains state dir so init_db writes to tmp.
    state = tmp_path / "brains-state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    # Force-reload settings so the new state dir takes effect.
    from brains import config as config_module

    config_module.reload_settings()
    yield home
    monkeypatch.delenv("BRAINS_STATE_DIR", raising=False)
    config_module.reload_settings()


def test_setup_dry_run_writes_nothing(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``setup --dry-run`` reports every step it would take but never
    invokes init_db or wire writers."""
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(app, ["setup", "--path", str(workspace), "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    steps = {s["step"] for s in payload["steps"]}
    assert {"init", "wire", "features_status"}.issubset(steps)
    init_step = next(s for s in payload["steps"] if s["step"] == "init")
    assert "would_do" in init_step
    # No DB should exist in the sandbox.
    assert not any((isolated_home.parent / "brains-state").rglob("*.db"))


def test_setup_full_run_is_idempotent(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: setup runs, DB exists, workspace registered, wire
    report present. Second invocation finds the same state."""
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    first = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["dry_run"] is False
    init_step = next(s for s in payload["steps"] if s["step"] == "init")
    assert init_step["workspace"]["path"]
    assert init_step["admin_key"]["source"] in ("generated", "existing")

    second = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    assert second.exit_code == 0, second.output
    payload2 = json.loads(second.output)
    init2 = next(s for s in payload2["steps"] if s["step"] == "init")
    # Same workspace, same slug — re-registration is a no-op.
    assert init2["workspace"]["slug"] == init_step["workspace"]["slug"]
    # Admin key already existed on the second run.
    assert init2["admin_key"]["source"] == "existing"


def test_setup_no_wire_skips_wire_step(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    wire_step = next(s for s in payload["steps"] if s["step"] == "wire")
    assert wire_step.get("skipped") is True


def test_setup_fails_closed_before_installing_incompatible_opencode_plugin(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brains import wire as wire_module

    (isolated_home / ".config/opencode").mkdir(parents=True)
    monkeypatch.setattr(
        wire_module,
        "_opencode_compatibility",
        lambda: (False, "OpenCode version could not be verified"),
    )
    workspace = tmp_path / "unsupported-opencode"
    workspace.mkdir()
    result = CliRunner().invoke(
        app,
        ["setup", "--path", str(workspace), "--transport", "stdio", "--json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    wire_step = next(step for step in payload["steps"] if step["step"] == "wire")
    assert wire_step["report"]["ok"] is False
    assert wire_step["report"]["tools"][0]["lifecycle_plugin"]["action"] == "error"
    assert not (isolated_home / ".config/opencode/plugins/brains-lifecycle.js").exists()
    assert not (isolated_home / ".config/opencode/opencode.json").exists()


def test_setup_rejects_bad_transport(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(
        app,
        ["setup", "--path", str(workspace), "--transport", "bogus", "--json"],
    )
    assert result.exit_code != 0
    assert "transport must be 'streamable-http'" in result.output


def test_mcp_command_defaults_to_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_mcp_server(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("brains.mcp.server.run_mcp_server", fake_run_mcp_server)
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "streamable-http"


def test_wire_cli_fails_when_codex_token_env_is_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_home / ".codex").mkdir()
    monkeypatch.setenv("BRAINS_API_KEY", "synthetic-effective-key")
    monkeypatch.delenv("BRAINS_MCP_BEARER_TOKEN", raising=False)
    from brains import config as config_module

    config_module.reload_settings()
    result = CliRunner().invoke(app, ["wire", "--tool", "codex", "--no-rules"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["tools"][0]["mcp"]["action"] == "error"
    assert "BRAINS_MCP_BEARER_TOKEN" in payload["tools"][0]["mcp"]["detail"]
    assert "synthetic-effective-key" not in result.output
    assert not (isolated_home / ".codex" / "config.toml").exists()
    assert list((isolated_home.parent / "brains-state").iterdir()) == []


def test_standalone_wire_refusal_creates_no_key_state_or_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "clean-home"
    state_dir = tmp_path / "clean-brains-state"
    (home / ".codex").mkdir(parents=True)
    state_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "BRAINS_STATE_DIR": str(state_dir),
        }
    )
    env.pop("BRAINS_API_KEY", None)
    env.pop("BRAINS_MCP_BEARER_TOKEN", None)
    env.pop("BRAINS_CONFIG", None)
    env.pop("BRAINS_RUNTIME_OVERLAY", None)

    result = subprocess.run(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "brains", "wire", "--tool", "codex"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["tools"][0]["mcp"]["action"] == "error"
    assert list(state_dir.iterdir()) == []
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".codex" / "AGENTS.md").exists()


def test_wire_cli_accepts_matching_synthetic_codex_token_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_home / ".codex").mkdir()
    monkeypatch.setenv("BRAINS_API_KEY", "synthetic-matching-key")
    monkeypatch.setenv("BRAINS_MCP_BEARER_TOKEN", "synthetic-matching-key")
    from brains import config as config_module

    config_module.reload_settings()
    result = CliRunner().invoke(app, ["wire", "--tool", "codex", "--no-rules"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    rendered = (isolated_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'url = "http://127.0.0.1:9877/mcp"' in rendered
    assert 'bearer_token_env_var = "BRAINS_MCP_BEARER_TOKEN"' in rendered
    assert "synthetic-matching-key" not in rendered


def test_setup_next_hint_present(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    """A no-wire bootstrap must not claim an MCP endpoint was configured."""
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["next"]["start_gateway"] == "brains-ai serve"
    assert payload["next"]["mcp_transport"] is None
    assert payload["next"]["mcp_url"] is None
    assert "tip" in payload["next"]


@pytest.mark.parametrize(
    ("transport_args", "expected_transport", "expected_suffix"),
    [
        ([], "streamable-http", "/mcp"),
        (["--transport", "streamable-http"], "streamable-http", "/mcp"),
        (["--transport", "sse"], "sse", "/sse"),
        (["--transport", "stdio"], "stdio", None),
    ],
)
def test_setup_report_is_transport_specific(
    isolated_home: Path,
    tmp_path: Path,
    transport_args: list[str],
    expected_transport: str,
    expected_suffix: str | None,
) -> None:
    (isolated_home / ".copilot").mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = CliRunner().invoke(
        app,
        ["setup", "--path", str(workspace), "--dry-run", "--json", *transport_args],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    wire = next(step for step in payload["steps"] if step["step"] == "wire")["report"]
    assert wire["transport"] == expected_transport
    assert payload["next"]["mcp_transport"] == expected_transport
    if expected_suffix is None:
        assert wire["url"] is None
        assert payload["next"]["mcp_url"] is None
    else:
        assert wire["url"].endswith(expected_suffix)
        assert payload["next"]["mcp_url"].endswith(expected_suffix)


@pytest.mark.parametrize(
    ("transport_args", "required", "forbidden"),
    [
        ([], ("Streamable HTTP", "/mcp", "BRAINS_MCP_BEARER_TOKEN"), ("/sse",)),
        (
            ["--transport", "streamable-http"],
            ("Streamable HTTP", "/mcp", "BRAINS_MCP_BEARER_TOKEN"),
            ("/sse",),
        ),
        (["--transport", "sse"], ("legacy SSE", "/sse"), ("/mcp",)),
        (
            ["--transport", "stdio"],
            ("stdio", "no HTTP endpoint or listener"),
            ("/mcp", "/sse", "127.0.0.1:9877"),
        ),
    ],
)
def test_setup_human_output_is_transport_specific(
    isolated_home: Path,
    tmp_path: Path,
    transport_args: list[str],
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    (isolated_home / ".copilot").mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = CliRunner().invoke(
        app,
        ["setup", "--path", str(workspace), "--dry-run", *transport_args],
    )
    assert result.exit_code == 0, result.output
    for text in required:
        assert text in result.output
    for text in forbidden:
        assert text not in result.output


def test_setup_default_output_is_human_readable(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    """Without ``--json``, ``setup`` prints a friendly progress summary,
    NOT a JSON blob. This guards the UX regression where 0.1.0a9 dumped
    raw JSON at users on first run.
    """
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire"])
    assert result.exit_code == 0, result.output

    # The output must NOT be parseable as a single JSON document — that
    # would mean we regressed and dumped the summary dict raw.
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)

    # The friendly format must surface the labelled sections + next-step.
    output = result.output
    assert "Brains setup" in output
    assert "[1/4] init" in output
    assert "[2/4] wire MCP" in output
    assert "[3/4] optional features" in output
    assert "[4/4] next steps" in output
    assert "brains-ai serve" in output
    assert "MCP wiring:" in output
    assert "/mcp" not in output
    assert "Console:" in output


def test_setup_json_flag_emits_machine_readable(
    isolated_home: Path,
    tmp_path: Path,
) -> None:
    """``setup --json`` must emit a single JSON document for scripts/CI."""
    runner = CliRunner()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(app, ["setup", "--path", str(workspace), "--no-wire", "--json"])
    assert result.exit_code == 0, result.output
    # MUST be one parseable JSON document — that's the contract.
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    assert "steps" in payload
    assert "next" in payload
