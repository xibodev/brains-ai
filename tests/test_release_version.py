from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_release_version.py"


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _run(tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), tag],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_tag_matches_package_version() -> None:
    version = _version()
    result = _run(f"v{version}")

    assert result.returncode == 0, result.stderr
    assert f"matches package version {version}" in result.stdout


def test_release_tag_mismatch_fails_closed() -> None:
    result = _run("v0.0.0")

    assert result.returncode != 0
    assert "does not match package version" in result.stderr
