from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependency_rejects_incompatible_mcp_major() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    mcp_requirement = next(
        dependency for dependency in project["dependencies"] if dependency.startswith("mcp")
    )
    assert "<2" in mcp_requirement


def test_runtime_image_healthchecks_every_default_supervised_surface() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8787/health" in dockerfile
    assert "mcp_protocol_status(timeout=3)['ready']" in dockerfile
    # The legacy dashboard is retired from the default serve-all topology.
    assert "for port in (9876,9877)" not in dockerfile
