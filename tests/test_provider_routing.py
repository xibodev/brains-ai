"""Integration tests for ``brains.router.model_router.select_model``.

Asserts the public select_model contract under the **new** faithful-
proxy semantics:

- Routing enabled (default) — classifier only fires for the explicit
  ``brains/auto`` alias. Tier aliases (``brains/cheap`` etc.), direct
  provider-catalog matches, and the Anthropic CLI shorthands all work
  without involving the classifier. Unknown ids raise
  :class:`ModelNotFoundError` (the API handlers convert to 404).
- Routing disabled — same as above, except ``brains/auto`` is refused
  too. The flag's job is solely to gate the classifier-driven alias.
"""

import pytest

from brains.config import ModelRoute, RouterConfig, settings
from brains.providers.registry import ProviderConfigError
from brains.router.classifier import classify
from brains.router.model_router import ModelNotFoundError, select_model


def test_explicit_brains_alias_with_misconfigured_provider_propagates(monkeypatch):
    """Tier-alias resolution still constructs the underlying provider
    eagerly — a misconfigured provider behind the alias must surface
    as :class:`ProviderConfigError` rather than be silently absorbed.
    """
    monkeypatch.setitem(settings.models, "cheap", ModelRoute(provider="missing", model="x"))
    classification = classify([{"role": "user", "content": "hi"}])
    with pytest.raises(ProviderConfigError):
        select_model(classification, requested_model="brains/cheap")


def test_router_disabled_unknown_model_raises(monkeypatch):
    """Routing off + unknown id → ``ModelNotFoundError``. We no longer
    silently coerce arbitrary client ids onto the default tier."""
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=False))
    classification = classify([{"role": "user", "content": "anything"}])
    with pytest.raises(ModelNotFoundError):
        select_model(classification, requested_model="some-upstream-model")


def test_router_disabled_no_model_raises(monkeypatch):
    """Routing off + no requested model → ``ModelNotFoundError``. The
    classifier never silently picks a model for the client."""
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=False))
    classification = classify([{"role": "user", "content": "anything"}])
    with pytest.raises(ModelNotFoundError):
        select_model(classification)


def test_router_disabled_tier_alias_still_works(monkeypatch):
    """``brains/cheap`` and friends are explicit client choices — they
    keep working with routing off. Only ``brains/auto`` (the alias that
    asks brains to classify) is gated."""
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=False))
    classification = classify([{"role": "user", "content": "anything"}])
    route = select_model(classification, requested_model="brains/cheap")
    assert route["tier"] == "cheap"
    assert route["model"] == settings.models["cheap"].model
    assert route["is_stub"] is True


def test_router_enabled_explicit_id_is_honoured_verbatim(monkeypatch):
    """The classifier never silently overrides an explicit client
    choice. Known catalog id → direct match, returned verbatim."""
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))
    classification = classify([{"role": "user", "content": "fix the failing test"}])
    # echo-default lives in the echo provider's catalog (default config).
    route = select_model(classification, requested_model="echo-default")
    assert route["model"] == "echo-default"
    assert route["resolution"] == "direct"
    assert route["is_stub"] is True


def test_router_enabled_brains_auto_classifies(monkeypatch):
    """The one path that consults the classifier is the explicit
    ``brains/auto`` alias. Tier comes from the classifier's pick."""
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))
    classification = classify([{"role": "user", "content": "anything"}])
    route = select_model(classification, requested_model="brains/auto")
    assert route["resolution"] == "alias"
    assert route["is_stub"] is True
    # The classifier picked some tier; the routed model must match that
    # tier's pinned model.
    assert route["model"] == settings.models[route["tier"]].model
