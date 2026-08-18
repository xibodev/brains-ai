"""Tests for ``brains-ai run <tool>``.

The runner replaces the current process via ``os.execvpe`` (POSIX and
Windows both supported by Python — Windows spawns a new PID and
terminates the parent). We monkeypatch ``os.execvpe`` to capture what
WOULD be launched without actually shelling out.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from brains.cli import run as run_mod
from brains.cli.app import app


def _fake_execvpe_capture():
    """Returns (captured_dict, callable). Callable raises SystemExit(0)
    so the runner's "execvpe never returns" defensive RuntimeError
    doesn't fire."""
    captured: dict[str, object] = {}

    def _fake(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    return captured, _fake


def test_run_unknown_tool_rejected():
    runner = CliRunner()
    result = runner.invoke(app, ["run", "not-a-real-tool"])
    assert result.exit_code != 0
    assert "Unknown tool" in result.output


def test_run_missing_binary_exits_127(monkeypatch: pytest.MonkeyPatch):
    """If the target tool isn't on PATH the runner exits 127 (the
    conventional 'command not found' code) instead of crashing."""
    monkeypatch.setattr(run_mod, "_default_api_key", lambda: "fake-key")
    monkeypatch.setattr(run_mod.shutil, "which", lambda _b: None)
    runner = CliRunner()
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 127
    assert "not found on PATH" in result.output


def test_run_claude_sets_anthropic_env_and_execs(monkeypatch: pytest.MonkeyPatch):
    """``brains-ai run claude`` resolves the binary, sets
    ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, and execs."""
    monkeypatch.setattr(run_mod, "_default_api_key", lambda: "test-admin-key")
    monkeypatch.setenv("BRAINS_GATEWAY_URL", "http://127.0.0.1:8787/v1")
    monkeypatch.setattr(run_mod.shutil, "which", lambda _b: "/usr/bin/claude")
    captured, fake = _fake_execvpe_capture()
    monkeypatch.setattr(run_mod.os, "execvpe", fake)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "claude"])
    assert result.exit_code == 0, result.output
    env = captured["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-admin-key"
    # gateway URL is /v1; ANTHROPIC_BASE_URL strips the /v1 suffix
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"


def test_run_copilot_sets_openai_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_mod, "_default_api_key", lambda: "test-admin-key")
    monkeypatch.setenv("BRAINS_GATEWAY_URL", "http://127.0.0.1:8787/v1")
    monkeypatch.setattr(run_mod.shutil, "which", lambda _b: "/usr/bin/copilot")
    captured, fake = _fake_execvpe_capture()
    monkeypatch.setattr(run_mod.os, "execvpe", fake)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "copilot"])
    assert result.exit_code == 0, result.output
    env = captured["env"]
    assert env["OPENAI_API_KEY"] == "test-admin-key"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8787/v1"


def test_run_forwards_extra_args(monkeypatch: pytest.MonkeyPatch):
    """``brains-ai run claude --resume foo`` forwards everything after
    the tool name to the tool unchanged."""
    monkeypatch.setattr(run_mod, "_default_api_key", lambda: "k")
    monkeypatch.setattr(run_mod.shutil, "which", lambda _b: "/usr/bin/claude")
    captured, fake = _fake_execvpe_capture()
    monkeypatch.setattr(run_mod.os, "execvpe", fake)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "claude", "--resume", "foo"])
    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    assert "--resume" in argv and "foo" in argv
    # argv[0] is the binary path.
    assert argv[0] == "/usr/bin/claude"


def test_brains_api_key_env_short_circuits_admin_key_lookup(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``BRAINS_API_KEY`` is set, the runner uses it verbatim
    instead of calling ``ensure_admin_key`` (which would pull in the
    full brains stack)."""
    monkeypatch.setenv("BRAINS_API_KEY", "explicit-key")

    def _boom(*_a, **_k):
        raise AssertionError("ensure_admin_key should not be called when BRAINS_API_KEY is set")

    monkeypatch.setattr("brains.api.admin_key.ensure_admin_key", _boom)
    assert run_mod._default_api_key() == "explicit-key"


def test_gateway_url_env_override_propagates(monkeypatch: pytest.MonkeyPatch):
    """``BRAINS_GATEWAY_URL`` lets the operator point the runner at a
    non-default gateway (e.g. a containerised brains on a different
    port)."""
    monkeypatch.setattr(run_mod, "_default_api_key", lambda: "k")
    monkeypatch.setenv("BRAINS_GATEWAY_URL", "http://10.0.0.5:9999/v1")
    monkeypatch.setattr(run_mod.shutil, "which", lambda _b: "/usr/bin/claude")
    captured, fake = _fake_execvpe_capture()
    monkeypatch.setattr(run_mod.os, "execvpe", fake)

    runner = CliRunner()
    runner.invoke(app, ["run", "claude"])
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:9999"
    runner.invoke(app, ["run", "copilot"])
    assert captured["env"]["OPENAI_BASE_URL"] == "http://10.0.0.5:9999/v1"
