"""Tests for the brains executor action-gate."""

from __future__ import annotations

import os
import threading
import time

import pytest

from brains.control.sessions import register_workspace
from brains.exec import gate, runner


def test_classify_local_actions_are_allowed():
    assert gate.classify("git", ["status"]).gate is False
    assert gate.classify("git", ["commit", "-m", "x"]).gate is False
    assert gate.classify("git", ["diff"]).gate is False
    assert gate.classify("ls", ["-la"]).gate is False
    assert gate.classify("pytest", ["-q"]).gate is False
    assert gate.classify("/usr/bin/cat", ["file"]).gate is False


def test_classify_outward_actions_are_gated():
    assert gate.classify("git", ["push", "origin", "main"]).gate is True
    assert gate.classify("gh", ["pr", "create"]).gate is True
    assert gate.classify("aws", ["s3", "rm", "s3://x"]).gate is True
    assert gate.classify("vercel", ["deploy"]).gate is True
    assert gate.classify("docker", ["push", "img"]).gate is True
    assert gate.classify("npm", ["publish"]).gate is True
    assert gate.classify("ssh", ["host", "rm -rf /"]).gate is True


def test_classify_closes_flag_prefixed_bypasses():
    """Hardening: a gated subcommand must be caught ANYWHERE in args, so
    flag-prefixed forms can't slip a push/deploy past the gate."""
    assert gate.classify("git", ["-C", "/repo", "push"]).gate is True
    assert gate.classify("git", ["--git-dir=/x", "push", "origin"]).gate is True
    assert gate.classify("git", ["-c", "user.name=x", "push"]).gate is True
    # basename normalisation: absolute path is classified by basename
    assert gate.classify("/usr/bin/git", ["push"]).gate is True
    # pip install (fetch+exec remote code) is gated
    assert gate.classify("pip", ["install", "requests"]).gate is True
    assert gate.classify("pip3", ["install", "-r", "req.txt"]).gate is True
    # extra cloud/deploy tools
    assert gate.classify("wrangler", ["deploy"]).gate is True
    assert gate.classify("stripe", ["charges", "create"]).gate is True


@pytest.mark.parametrize(
    "argv",
    [
        ["pip", "install", "requests"],
        ["tool", "install", "ruff"],
        ["tool", "upgrade", "ruff"],
        ["tool", "run", "ruff", "check"],
        ["run", "python", "deploy.py"],
        ["run", "--with", "requests", "python", "-c", "print(1)"],
        ["add", "requests"],
        ["remove", "requests"],
        ["sync"],
        ["build"],
        ["publish"],
        ["python", "install", "3.12"],
        ["self", "update"],
        ["--directory", "/repo", "pip", "install", "requests"],
    ],
)
def test_uv_fetch_and_execute_shapes_are_gated(argv):
    """``uv`` multiplexes the shapes that ``pip install``/``uvx`` are gated for.

    Every one of these resolves a name against a remote index and then runs
    what came back - a build backend, a console script, the project's own
    entrypoint - so leaving them local would let an agent fetch and execute
    arbitrary code with the boundary reporting nothing.
    """
    assert gate.classify("uv", argv).gate is True, argv
    assert gate.classify("uvw", argv).gate is True, argv
    # The wrapped form is the same decision.
    assert gate.classify("sudo", ["uv", *argv]).gate is True, argv
    assert gate.classify("python", ["-m", "uv", *argv]).gate is True, argv


@pytest.mark.parametrize(
    "argv",
    [
        ["pip", "list"],
        ["pip", "show", "requests"],
        ["pip", "freeze"],
        ["pip", "check"],
        ["pip", "tree"],
        ["tool", "list"],
        ["tool", "dir"],
        ["python", "list"],
        ["python", "find"],
        ["cache", "dir"],
        ["tree"],
        ["export"],
        ["version"],
        ["lock", "--check"],
    ],
)
def test_read_only_uv_commands_stay_local(argv):
    """Over-gating is cheap but not free: a read-only command must not ASK."""
    assert gate.classify("uv", argv).gate is False, argv


def test_strict_mode_gates_unknown_binaries(monkeypatch):
    monkeypatch.setenv("BRAINS_GATE_MODE", "strict")  # unknown binary -> gated in strict
    assert gate.classify("some-weird-tool", ["--do-it"]).gate is True
    # known-local stays allowed even in strict
    assert gate.classify("ls", ["-la"]).gate is False
    assert gate.classify("python", ["-m", "pytest"]).gate is False
    assert gate.classify("git", ["status"]).gate is False


def test_standard_mode_allows_unknown(monkeypatch):
    monkeypatch.delenv("BRAINS_GATE_MODE", raising=False)
    assert gate.classify("some-weird-tool", ["--do-it"]).gate is False


def test_classify_network_fetch_local_vs_remote():
    assert gate.classify("curl", ["http://localhost:8787/x"]).gate is False
    assert gate.classify("curl", ["http://127.0.0.1:9999"]).gate is False
    assert gate.classify("curl", ["https://api.stripe.com/charges"]).gate is True
    assert gate.classify("wget", ["https://example.com/x"]).gate is True


def test_install_shims_only_for_present_binaries(tmp_path, monkeypatch):
    # A fake PATH with just a couple of executables.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "ls"):
        f = bindir / name
        f.write_text("#!/bin/sh\n", encoding="utf-8")
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    shim_dir = tmp_path / "shims"
    written = runner.install_shims(
        shim_dir, binaries=["git", "vercel"], python="python3", style="posix"
    )
    assert "git" in written
    assert "vercel" not in written  # not on PATH -> not shimmed
    assert (shim_dir / "git").exists()
    assert os.access(shim_dir / "git", os.X_OK)


def test_install_shims_writes_windows_cmd_wrappers(tmp_path, monkeypatch):
    """A POSIX-only shim never intercepts anything on Windows.

    ``PATHEXT`` resolution is what makes a shim shadow ``git`` there, so the
    installer has to write ``git.cmd``; writing an extensionless ``#!/bin/sh``
    file would leave the gate silently uninstalled on a Windows host.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    shim_dir = tmp_path / "winshims"

    written = runner.install_shims(
        shim_dir, binaries=["git"], python=r"C:\Python\python.exe", style="windows"
    )

    assert written == ["git"]
    script = shim_dir / "git.cmd"
    assert script.exists()
    body = script.read_text(encoding="utf-8")
    assert "-m brains.exec.gate git %*" in body


def test_gate_authorization_releases_an_approved_action(monkeypatch, tmp_path):
    """The canonical governed path: file -> approve -> consume -> allowed."""
    from brains.control.decisions import list_open_decisions, resolve_decision
    from brains.govern import (
        STATUS_AUTHORIZED,
        TIER_OUTWARD,
        ActionTarget,
        GovernedRequest,
        authorize,
    )

    workspace = tmp_path / "ws-approve"
    workspace.mkdir()
    # Register up front: both this thread and the resolver thread would
    # otherwise race to create the same workspace row.
    register_workspace(str(workspace))
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="git",
        args=["push", "origin", "main"],
        target=ActionTarget(workspace_path=str(workspace)),
        tier=TIER_OUTWARD,
        summary="git push origin main",
    )

    def _approve_soon() -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            pending = list_open_decisions(workspace_path=str(workspace))
            if pending:
                resolve_decision(pending[0]["code"], chosen="approve", reasoning="ok")
                return
            time.sleep(0.05)

    resolver = threading.Thread(target=_approve_soon, daemon=True)
    resolver.start()
    decision = authorize(request, poll_seconds=0.05, timeout_seconds=15, notify=False)
    resolver.join(timeout=5)

    assert decision.allowed is True
    assert decision.status == STATUS_AUTHORIZED
    assert decision.approval_code


def test_gate_authorization_blocks_a_rejected_action(monkeypatch, tmp_path):
    from brains.control.decisions import list_open_decisions, resolve_decision
    from brains.govern import (
        STATUS_DENIED,
        TIER_OUTWARD,
        ActionTarget,
        GovernedRequest,
        authorize,
    )

    workspace = tmp_path / "ws-deny"
    workspace.mkdir()
    register_workspace(str(workspace))
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="vercel",
        args=["deploy", "--prod"],
        target=ActionTarget(workspace_path=str(workspace)),
        tier=TIER_OUTWARD,
        summary="vercel deploy --prod",
    )

    def _deny_soon() -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            pending = list_open_decisions(workspace_path=str(workspace))
            if pending:
                resolve_decision(
                    pending[0]["code"], chosen="deny", reasoning="no", status="rejected"
                )
                return
            time.sleep(0.05)

    resolver = threading.Thread(target=_deny_soon, daemon=True)
    resolver.start()
    decision = authorize(request, poll_seconds=0.05, timeout_seconds=15, notify=False)
    resolver.join(timeout=5)

    assert decision.allowed is False
    assert decision.status == STATUS_DENIED


def test_decision_store_roundtrip(tmp_path):
    """End-to-end (single thread, real store): file -> get(open) -> resolve -> get(resolved)."""
    from brains.control.decisions import file_decision_request, get_decision, resolve_decision

    repo = tmp_path / "ws_rt"
    repo.mkdir()
    filed = file_decision_request(str(repo), title="[gate] approve outward action: git push")
    code = filed["code"]
    assert get_decision(code)["status"] == "open"
    resolve_decision(code, chosen="approve", reasoning="ok", status="resolved")
    state = get_decision(code)
    assert state["status"] == "resolved"
    assert state["chosen"] == "approve"
