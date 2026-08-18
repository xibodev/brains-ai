"""BL-P0-05 - the console's half of the Session control contract.

A message sent and a stop pressed are network requests, and the operator does
not wait for them: they click the next Session while the first one's request is
still in flight. When it resolves, the dock is showing something else, and a
result applied to whatever happens to be on screen puts one Session's message
bubble, toast or capability into another Session's thread - a fabrication the
durable queue on the server cannot prevent, because the server answered
correctly.

The rule (capture the Session at request time, ignore the answer when the
selection moved on, hold pending state per Session) lives in
``frontend/src/components/sessionScope.ts`` and is asserted by Node's built-in
test runner, which needs no test dependency the repository does not already
imply. This module runs that suite from the Python gate so a regression in the
browser half is caught by the same command as a regression in the server half.
It skips - rather than fails - where Node is absent or too old to strip
TypeScript types, because the Python gate does not own the Node toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SUITE = Path("src/components/sessionScope.test.ts")
DOCK = Path("src/components/ChatDock.tsx")

#: Node strips TypeScript types without a flag from 22.18 onward.
MIN_NODE = (22, 18)


def _node() -> str | None:
    return shutil.which("node")


def _node_version(node: str) -> tuple[int, ...]:
    try:
        raw = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [node, "--version"], capture_output=True, text=True, timeout=60, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return ()
    return tuple(int(part) for part in raw.lstrip("v").split(".") if part.isdigit())


@pytest.mark.skipif(not (FRONTEND / SUITE).exists(), reason="frontend sources are not present")
def test_frontend_session_scope_suite_passes():
    node = _node()
    if node is None:
        pytest.skip("node is not installed")
    version = _node_version(node)
    if version < MIN_NODE:
        pytest.skip(f"node {version or 'unknown'} cannot strip TypeScript types")

    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell
        [node, "--test", "--test-reporter=tap", str(SUITE)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "not ok" not in completed.stdout


@pytest.mark.skipif(not (FRONTEND / DOCK).exists(), reason="frontend sources are not present")
def test_the_dock_scopes_every_async_result_to_the_session_it_asked_for():
    """The rules are only worth having if the dock actually applies them.

    Asserted on the source because the SPA has no component test runner: what
    matters is that no ``await``ed Session response reaches component state
    without passing the scope guard, and that pending and stopping state are
    keyed by Session rather than held in one slot the wrong Session can fill.
    """
    source = (FRONTEND / DOCK).read_text(encoding="utf-8")
    assert 'from "./sessionScope"' in source
    # Pending sends and the stop-in-flight flag are per Session.
    assert "ScopedState<ThreadMsg[]>" in source
    assert "stoppingSession" in source
    assert "setStopping(" not in source
    # Every response is applied through the guard rather than unconditionally.
    assert "isCurrent(currentSession" in source
    assert source.count("isCurrent(currentSession") >= 5
    assert ".then(setDetail)" not in source


@pytest.mark.skipif(not (FRONTEND / SUITE).exists(), reason="frontend sources are not present")
def test_the_test_suite_is_excluded_from_the_spa_build():
    """``tsc``/``vite`` must not try to compile a Node-only test file."""
    config = json.loads(
        "\n".join(
            line
            for line in (FRONTEND / "tsconfig.json").read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("//")
        )
    )
    assert "src/**/*.test.ts" in config.get("exclude", [])
