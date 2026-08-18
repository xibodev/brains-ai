"""Tests for the runtime overlay + ``${ENV:NAME}`` resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from brains.config import (
    ADMIN_EDITABLE_KEYS,
    ENV_REF_ALLOWED_FIELDS,
    _resolve_env_refs,
    load_settings,
    reload_settings,
    settings,
)


@pytest.fixture
def chdir_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Clear any prior overlay so the loader sees a clean slate.
    overlay = tmp_path / "brains.runtime.yaml"
    if overlay.exists():
        overlay.unlink()
    yield tmp_path
    # Tear down before ``reload_settings`` so the baseline is restored.
    if overlay.exists():
        overlay.unlink()
    monkeypatch.delenv("BRAINS_RUNTIME_OVERLAY", raising=False)
    reload_settings()


def test_env_ref_resolves_scalars(monkeypatch):
    monkeypatch.setenv("BRAINS_TEST_SECRET", "s3cr3t")
    assert _resolve_env_refs("${ENV:BRAINS_TEST_SECRET}") == "s3cr3t"
    # Unmatched leaves value intact.
    assert _resolve_env_refs("plain") == "plain"
    # Missing env var resolves to None.
    monkeypatch.delenv("BRAINS_MISSING", raising=False)
    assert _resolve_env_refs("${ENV:BRAINS_MISSING}") is None


def test_env_ref_walks_nested():
    os.environ["BRAINS_FOO"] = "bar"
    try:
        result = _resolve_env_refs(
            {"openai_compatible_api_key": "${ENV:BRAINS_FOO}", "rate_limit_per_minute": 60}
        )
        assert result["openai_compatible_api_key"] == "bar"
        assert result["rate_limit_per_minute"] == 60
    finally:
        os.environ.pop("BRAINS_FOO", None)


def test_overlay_merges_on_top_of_baseline(chdir_tmp: Path, monkeypatch):
    overlay = chdir_tmp / "brains.runtime.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "rate_limit_per_minute": 99,
                "models": {
                    "default": {"provider": "echo", "model": "from-overlay"},
                },
            }
        )
    )
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    fresh = load_settings()
    assert fresh.rate_limit_per_minute == 99
    assert fresh.models["default"].model == "from-overlay"


def test_overlay_ignores_keys_outside_allowlist(chdir_tmp: Path, monkeypatch):
    overlay = chdir_tmp / "brains.runtime.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "api_key": "hacker-attempt",  # NOT in the allowlist
                "rate_limit_per_minute": 7,
            }
        )
    )
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    fresh = load_settings()
    assert fresh.api_key != "hacker-attempt"
    assert fresh.rate_limit_per_minute == 7


def test_overlay_resolves_env_refs(chdir_tmp: Path, monkeypatch):
    overlay = chdir_tmp / "brains.runtime.yaml"
    overlay.write_text(yaml.safe_dump({"openai_compatible_api_key": "${ENV:BRAINS_TEST_OAI_KEY}"}))
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    monkeypatch.setenv("BRAINS_TEST_OAI_KEY", "the-real-key")
    fresh = load_settings()
    assert fresh.openai_compatible_api_key == "the-real-key"


def test_admin_editable_keys_lists_safe_fields_only():
    forbidden = {"api_key", "api_keys", "db_url", "allow_unauthenticated_api"}
    assert not (forbidden & ADMIN_EDITABLE_KEYS)


def test_reload_mutates_existing_singleton(chdir_tmp: Path, monkeypatch):
    overlay = chdir_tmp / "brains.runtime.yaml"
    overlay.write_text(yaml.safe_dump({"rate_limit_per_minute": 42}))
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    refreshed = reload_settings()
    # Both the returned object and the module-level singleton are updated.
    assert refreshed is settings
    assert settings.rate_limit_per_minute == 42


# ---------- FIX-002 negative tests: env-refs only at allow-listed fields ----------


def test_env_ref_only_allowed_at_listed_fields():
    # The allow-list must stay tight so non-secret fields can't be used
    # as an env-var exfiltration channel.
    assert "openai_compatible_api_key" in ENV_REF_ALLOWED_FIELDS
    # Non-secret fields explicitly NOT on the list.
    for field in ("models", "routes", "ollama_base_url", "rate_limit_per_minute"):
        assert field not in ENV_REF_ALLOWED_FIELDS


def test_env_ref_in_models_field_is_not_resolved_at_load(chdir_tmp: Path, monkeypatch):
    """An overlay smuggling ``${ENV:NAME}`` into models must NOT resolve it."""
    overlay = chdir_tmp / "brains.runtime.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "default": {
                        "provider": "echo",
                        "model": "${ENV:BRAINS_SECRET_LEAK}",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("BRAINS_RUNTIME_OVERLAY", str(overlay))
    monkeypatch.setenv("BRAINS_SECRET_LEAK", "should-not-leak")
    fresh = load_settings()
    # The model name is the literal placeholder, not the env-var value.
    assert fresh.models["default"].model == "${ENV:BRAINS_SECRET_LEAK}"
    assert fresh.models["default"].model != "should-not-leak"
