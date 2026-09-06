"""F3.4 — the non-negotiable GATE integration test (PRODUCT-SPEC F3.4, the moat).

Spawns a REAL subprocess that invokes a gated outward action through the brains
PATH shim and asserts the action is **intercepted -> queued (an ASK is filed) ->
blocked** until the operator resolves it, then either runs (approve) or is denied
(exit 13). This is the proof the gate actually stops outward actions in a live
session — not just that the classifier returns True.

POSIX only: the shims are ``#!/bin/sh`` wrappers and ``gate_main`` uses
``os.execv``. Skipped on Windows (the gate's target is Linux runtimes); run in a
Linux container / CI. The subprocess shares the conftest-isolated tmp DB via the
inherited ``BRAINS_DB_URL`` / ``BRAINS_STATE_DIR`` env, so the ASK it files is
visible to (and resolvable by) this test process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from brains.control.decisions import list_open_decisions, resolve_decision
from brains.control.operators import ensure_admin_operator
from brains.exec import runner

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX gate shims (#!/bin/sh + os.execv) — run on Linux"
)


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    yield


def _make_fake_git(realbin) -> None:
    """A stand-in git that just announces it ran (so 'the real binary executed'
    is observable without performing a network push)."""
    realbin.mkdir(parents=True, exist_ok=True)
    git = realbin / "git"
    git.write_text('#!/bin/sh\necho REAL_GIT_RAN "$@"\n', encoding="utf-8")
    git.chmod(0o755)


def _resolver(workspace_path: str, chosen: str, found: dict) -> None:
    """Background: wait for the gate ASK to appear (proves intercept+queue), then
    resolve it the given way (unblocks the subprocess)."""
    deadline = time.time() + 20
    while time.time() < deadline:
        pending = [
            d
            for d in list_open_decisions(workspace_path=workspace_path)
            if "outward action" in (d.get("title") or "")
        ]
        if pending:
            found["code"] = pending[0]["code"]
            resolve_decision(pending[0]["code"], chosen=chosen, reasoning=chosen, status="resolved")
            return
        time.sleep(0.2)


def _run_gated_push(tmp_path, monkeypatch, chosen: str) -> tuple[int, str, dict]:
    ws = tmp_path / "ws"
    ws.mkdir()
    realbin = tmp_path / "realbin"
    _make_fake_git(realbin)
    shim_dir = tmp_path / "shims"

    # Put the fake git on PATH so install_shims + the gate's _real_binary resolve
    # to it (the shim dir goes FIRST so the shim intercepts).
    base_env = dict(os.environ)
    base_env["PATH"] = f"{realbin}{os.pathsep}{base_env.get('PATH', '')}"
    monkeypatch.setenv("PATH", base_env["PATH"])

    written = runner.install_shims(shim_dir, binaries=["git"], python=sys.executable)
    assert "git" in written, "git shim was not installed"

    env = runner.build_gated_env(shim_dir, str(ws), session_id=None, base_env=base_env)

    found: dict = {}
    resolver = threading.Thread(target=_resolver, args=(str(ws), chosen, found), daemon=True)
    resolver.start()

    proc = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(ws),
        env=env,
        text=True,
        capture_output=True,
        timeout=40,
    )
    resolver.join(timeout=5)
    return proc.returncode, proc.stdout + proc.stderr, found


def test_gate_intercepts_queues_and_releases_on_approve(tmp_path, monkeypatch):
    """Approve path: the outward `git push` is intercepted, an ASK is filed and
    blocks, and once approved the REAL binary runs."""
    rc, output, found = _run_gated_push(tmp_path, monkeypatch, "approve")

    assert found.get("code"), "no approval ASK was filed — the action was NOT gated"
    assert "REAL_GIT_RAN push origin main" in output, (
        "approved action did not reach the real binary"
    )
    assert rc == 0, f"approved push should exit 0, got {rc}: {output}"


def test_gate_blocks_and_denies(tmp_path, monkeypatch):
    """Deny path: the same action is intercepted and, when denied, is BLOCKED —
    the real binary never runs and the shim exits non-zero."""
    rc, output, found = _run_gated_push(tmp_path, monkeypatch, "deny")

    assert found.get("code"), "no approval ASK was filed — the action was NOT gated"
    assert "REAL_GIT_RAN" not in output, "denied action LEAKED to the real binary (gate breach!)"
    assert rc == 13, f"denied push should exit 13, got {rc}: {output}"
