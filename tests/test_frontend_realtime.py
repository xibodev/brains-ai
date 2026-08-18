"""BL-P0-02 - the console's half of the realtime contract.

The server derives every topic name and hands back a monotonic cursor; the
console has to route frames under the *derived* name, resume from its cursor on
reconnect, apply each durable event once, and treat a reset as "resynchronise
from REST" rather than as a frame to ignore. Those rules live in
``frontend/src/realtime/protocol.ts`` and are asserted by Node's built-in test
runner, which needs no test dependency the repository does not already imply.

This module runs that suite from the Python gate so a regression in the browser
half is caught by the same command as a regression in the server half. It skips
- rather than fails - where Node is absent or too old to strip TypeScript
types, because the Python gate does not own the Node toolchain.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SUITE = Path("src/realtime/protocol.test.ts")

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
def test_frontend_realtime_protocol_suite_passes():
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
