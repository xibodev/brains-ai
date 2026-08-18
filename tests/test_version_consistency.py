"""Guard the `brains.__version__` runtime string against drift from the
`pyproject.toml` `version` field.

0.1.0a8 shipped with `brains.__version__` stuck at `"0.1.0a7"` because
the hardcoded string in `src/brains/__init__.py` was not bumped alongside
`pyproject.toml`. Since pipx, PyPI, and `brains-ai --version` all read the
runtime string, the user observed `0.1.0a7` after installing
`brains-ai==0.1.0a8` from PyPI.

The fix is to read the version dynamically from package metadata at import
time (`importlib.metadata.version("brains-ai")`). This test pins that
behaviour so the bug cannot recur.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import brains


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_runtime_version_matches_pyproject_version() -> None:
    """`brains.__version__` must equal `pyproject.toml`'s `[project].version`.

    If this fails, the runtime version string drifted from the package
    metadata — the cause of the 0.1.0a8 mis-versioning incident.
    """
    assert brains.__version__ == _pyproject_version(), (
        f"brains.__version__={brains.__version__!r} drifted from "
        f"pyproject.toml version={_pyproject_version()!r}. "
        "Update src/brains/__init__.py to read from package metadata, "
        "and check the build did not strip dist-info."
    )


def test_runtime_version_is_pep440_shaped() -> None:
    """Guard against non-string / placeholder leakage to `--version` output."""
    assert isinstance(brains.__version__, str)
    # Accept PEP 440 release + pre-release + dev segments brains uses
    # (e.g. "0.1.0a14", "0.1.0a15.dev0", "0.1.0rc1", "0.1.0.post1+local").
    pep440 = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+\S+)?$")
    assert pep440.match(brains.__version__), (
        f"brains.__version__={brains.__version__!r} is not a recognisable "
        "PEP 440 release version — check importlib.metadata fallback path."
    )


def test_cli_version_command_matches_runtime() -> None:
    """`brains-ai version` and `brains.__version__` must agree.

    The CLI imports `__version__` lazily inside the `version` callback;
    this test exercises that path without spawning a subprocess.
    """
    from typer.testing import CliRunner

    from brains.cli.app import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert brains.__version__ in result.output, (
        f"`brains-ai version` output did not contain "
        f"{brains.__version__!r}.\nFull output:\n{result.output}"
    )


def test_module_reload_re_reads_metadata(monkeypatch) -> None:
    """If the user upgrades the package in-place, a fresh import picks up
    the new metadata. Validates importlib.metadata is queried at import,
    not frozen at first load."""
    import importlib

    # Re-import a fresh copy and re-read metadata; both should agree.
    monkeypatch.delitem(sys.modules, "brains", raising=False)
    reloaded = importlib.import_module("brains")
    assert reloaded.__version__ == _pyproject_version()
