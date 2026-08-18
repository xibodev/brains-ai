"""Tests for the optional-extras registry and fail-loud subsystem gate.

Covers:
- The extras registry mirrors what's declared in pyproject.toml.
- ``require_extra`` raises with a remediation hint when the extra is missing.
- ``load_settings`` fails loud at startup if a subsystem is enabled but its
  extra is not installed.
- A bad ``schema_version`` in the overlay is rejected.
- The lean-core default config loads cleanly with no extras enabled.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from brains import config as config_module
from brains.extras import (
    EXTRAS,
    ExtraNotInstalledError,
    installed_extras,
    is_extra_installed,
    require_extra,
)

# --- Extras registry ---------------------------------------------------------


def test_extras_registry_mirrors_pyproject() -> None:
    """Every extra in pyproject.toml [project.optional-dependencies] should be
    declared in :data:`EXTRAS` (excluding ``dev`` and the meta-extra ``all``).
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # Crude parse: find top-level keys under [project.optional-dependencies].
    section_start = text.index("[project.optional-dependencies]")
    section_end = text.index("\n[", section_start + 1)
    block = text[section_start:section_end]
    declared = set()
    for line in block.splitlines():
        if "=" in line and not line.strip().startswith(("#", "[", '"')):
            name = line.split("=", 1)[0].strip()
            if name and name not in ("dev", "all"):
                declared.add(name)
    assert declared == set(EXTRAS), f"pyproject declares {declared}, EXTRAS declares {set(EXTRAS)}"


def test_installed_extras_returns_a_bool_for_every_extra() -> None:
    snapshot = installed_extras()
    assert set(snapshot) == set(EXTRAS)
    for value in snapshot.values():
        assert isinstance(value, bool)


def test_is_extra_installed_returns_false_for_unknown_name() -> None:
    assert is_extra_installed("does-not-exist") is False


# --- require_extra fail-loud helper -----------------------------------------


def test_require_extra_raises_with_install_hint_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error message must contain the exact ``pip install`` command."""
    # Force the probe to fail by removing the module from sys.modules and
    # also blocking its import path. The telegram probe imports ``telegram``.
    monkeypatch.setitem(sys.modules, "telegram", None)
    with pytest.raises(ExtraNotInstalledError) as excinfo:
        require_extra("telegram", "subsystems.bridges.telegram")
    msg = str(excinfo.value)
    assert "telegram" in msg
    assert "pip install 'brains-ai[telegram]'" in msg
    assert "subsystems.bridges.telegram" in msg


def test_require_extra_unknown_name_raises() -> None:
    with pytest.raises(ExtraNotInstalledError) as excinfo:
        require_extra("not-a-real-extra", "subsystems.fake")
    assert "Unknown brains extra" in str(excinfo.value)


def test_require_extra_passes_when_module_importable() -> None:
    """``httpx`` ships in the lean core so ``whatsapp`` (which probes nothing)
    must always pass. We use it here as a no-probe sanity check.
    """
    # whatsapp has empty probe_modules, so it always "passes"
    require_extra("whatsapp", "subsystems.bridges.whatsapp")


# --- Subsystem gate enforced at load_settings -------------------------------


@pytest.fixture
def tmp_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a temp overlay path and point BRAINS_RUNTIME_OVERLAY at it."""
    overlay = tmp_path / "brains.runtime.yaml"
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    return overlay


def test_load_settings_lean_core_succeeds_with_no_overlay(
    tmp_overlay: Path,
) -> None:
    fresh = config_module.load_settings()
    assert fresh.subsystems.storage.backend == "sqlite"
    assert fresh.subsystems.bridges.telegram.enabled is False
    assert fresh.subsystems.otel.enabled is False


def test_load_settings_fails_loud_when_telegram_enabled_without_extra(
    tmp_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "telegram", None)
    tmp_overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": config_module.RUNTIME_OVERLAY_SCHEMA_VERSION,
                "subsystems": {"bridges": {"telegram": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtraNotInstalledError) as excinfo:
        config_module.load_settings()
    assert "pip install 'brains-ai[telegram]'" in str(excinfo.value)


def test_load_settings_fails_loud_when_postgres_backend_without_extra(
    tmp_overlay: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    tmp_overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": config_module.RUNTIME_OVERLAY_SCHEMA_VERSION,
                "subsystems": {"storage": {"backend": "postgres"}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtraNotInstalledError) as excinfo:
        config_module.load_settings()
    assert "pip install 'brains-ai[postgres]'" in str(excinfo.value)


def test_load_settings_rejects_unknown_schema_version(tmp_overlay: Path) -> None:
    tmp_overlay.write_text(
        yaml.safe_dump({"schema_version": 999, "models": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        config_module.load_settings()
    assert "schema_version" in str(excinfo.value)


def test_load_settings_accepts_overlay_without_schema_version(
    tmp_overlay: Path,
) -> None:
    """Older overlays predate the schema_version field. They must still load
    so we don't break operators who upgrade with an existing config.
    """
    tmp_overlay.write_text(
        yaml.safe_dump({"rate_limit_per_minute": 42}),
        encoding="utf-8",
    )
    fresh = config_module.load_settings()
    assert fresh.rate_limit_per_minute == 42
