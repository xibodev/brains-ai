"""Tests for the capability-aware orientation policy (from the model-ladder study)."""

from __future__ import annotations

from brains.exec.orient_policy import should_orient


def test_weak_and_cheap_models_get_oriented():
    for m in [
        "claude-haiku-4.5",
        "haiku",
        "gpt-5-mini",
        "gpt-4o-mini",
        "qwen2.5-coder:0.5b",
        "qwen2.5-coder:7b",
        "llama3.2:3b",
        "gemini-2.5-flash",
        "phi-3",
        "gemma2:2b",
    ]:
        assert should_orient(m) is True, m


def test_strong_navigators_skip_orientation():
    for m in [
        "claude-sonnet-4.5",
        "sonnet",
        "claude-opus-4",
        "opus",
        "gpt-5-codex",
        "codex",
        "gpt-5.1",
        "o3",
        "gemini-2.5-pro",
    ]:
        assert should_orient(m) is False, m


def test_no_model_defaults_to_skip():
    # No pinned model = the CLI's strong default → don't inject.
    assert should_orient(None) is False
    assert should_orient("") is False


def test_unknown_model_defaults_to_inject():
    # Safe asymmetry: cost vs correctness.
    assert should_orient("some-new-model-x") is True
