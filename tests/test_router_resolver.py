"""Tests for ``brains.router.resolver`` — model-name resolution.

Resolver covers four lookup paths (NEW contract, post-faithful-proxy
rewrite):

1. Brains tier alias (slash + hyphen + ``claude-``-prefixed). The
   special ``brains/auto`` requires ``allow_classifier=True``.
2. Direct match against enabled provider ``list_models()`` catalogs,
   including the ``<provider>/<model>`` namespaced form which pins
   both the provider AND the model.
3. Anthropic CLI shorthand (``sonnet``/``opus``/``haiku``/``fable``/
   ``best`` and the bare ``default``). Real ``claude-<family>-<ver>``
   ids fall through to the catalog at step 2 first.
4. Unknown id → :class:`ModelNotFoundError` (NEW: no silent classifier
   fallback). The API handlers convert this into a 404 so clients
   never have a request silently re-routed to a different model.

All four are exercised here, plus the ``normalize_model_name`` helper.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from brains.config import ModelRoute, RouterConfig, settings
from brains.router.classifier import classify
from brains.router.resolver import ModelNotFoundError, normalize_model_name, resolve_model

# ----- normalize_model_name ------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("Claude-Sonnet-4.5", "claude-sonnet-4-5"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("claude-sonnet-4.5[1m]", "claude-sonnet-4-5"),
        ("claude-sonnet-4.5[200k]", "claude-sonnet-4-5"),
        ("claude-sonnet-4.5 [1M]", "claude-sonnet-4-5"),
        ("GPT-4o-Mini", "gpt-4o-mini"),
        ("gpt-4o (preview)", "gpt-4o"),
        ("  brains-AUTO ", "brains-auto"),
    ],
)
def test_normalize_model_name(raw, expected):
    assert normalize_model_name(raw) == expected


# ----- alias paths --------------------------------------------------------


@pytest.fixture
def _classification():
    return classify([{"role": "user", "content": "do something simple"}])


@pytest.fixture
def _router_enabled(monkeypatch):
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))


@pytest.fixture
def _router_disabled(monkeypatch):
    monkeypatch.setattr(settings, "router", RouterConfig(enabled=False))


def _fake_factory(provider_obj=None):
    """Return a providers_factory that yields a benign stub."""

    def _make(_name: str):
        return provider_obj or SimpleNamespace(list_models=lambda: [])

    return _make


def test_resolve_tier_alias_brains_default(_router_enabled, _classification):
    route = resolve_model("brains-default", _classification, providers_factory=_fake_factory())
    assert route["tier"] == "default"
    assert route["model"] == settings.models["default"].model
    assert route["resolution"] == "alias"


def test_resolve_tier_alias_brains_strong(_router_enabled, _classification):
    route = resolve_model("brains-strong", _classification, providers_factory=_fake_factory())
    assert route["tier"] == "strong"
    assert route["model"] == settings.models["strong"].model
    assert route["resolution"] == "alias"


def test_resolve_slash_alias_brains_cheap(_router_enabled, _classification):
    """Slash form (canonical) and hyphen form (legacy) both resolve to
    the same tier."""
    slash = resolve_model("brains/cheap", _classification, providers_factory=_fake_factory())
    hyphen = resolve_model("brains-cheap", _classification, providers_factory=_fake_factory())
    assert slash["tier"] == hyphen["tier"] == "cheap"
    assert slash["model"] == hyphen["model"]


def test_resolve_claude_prefixed_alias_picked_up(_router_enabled, _classification):
    """Claude Code's discovery filter only adds ``claude*`` ids — so the
    claude-prefixed aliases must resolve identically to the bare ones."""
    bare = resolve_model("brains-cheap", _classification, providers_factory=_fake_factory())
    prefixed = resolve_model(
        "claude-brains-cheap", _classification, providers_factory=_fake_factory()
    )
    assert prefixed["tier"] == bare["tier"] == "cheap"
    assert prefixed["model"] == bare["model"]


def test_resolve_brains_auto_uses_classifier(_router_enabled, _classification):
    route = resolve_model("brains-auto", _classification, providers_factory=_fake_factory())
    assert route["resolution"] == "alias"
    assert route["model"] in {r.model for r in settings.models.values()}


def test_resolve_brains_auto_refused_when_classifier_disallowed(_classification):
    """``brains/auto`` is the one alias that asks brains to pick on the
    client's behalf — when ``allow_classifier=False`` it must raise so
    the client sees a 404 instead of a silent classification."""
    with pytest.raises(ModelNotFoundError):
        resolve_model(
            "brains-auto",
            _classification,
            providers_factory=_fake_factory(),
            allow_classifier=False,
        )
    with pytest.raises(ModelNotFoundError):
        resolve_model(
            "brains/auto",
            _classification,
            providers_factory=_fake_factory(),
            allow_classifier=False,
        )


def test_resolve_brains_auto_refuses_github_copilot_classifier_route(_router_enabled, monkeypatch):
    classification = SimpleNamespace(task_type="docs_lookup", recommended_model_tier="default")
    provider = SimpleNamespace(list_models=lambda: [{"id": "gpt-5", "vendor": "github"}])

    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="github_copilot", model="gpt-5"),
    )
    monkeypatch.setitem(settings.routes, "docs_lookup", "default")

    def factory(name: str):
        assert name == "github_copilot"
        return provider

    with pytest.raises(ModelNotFoundError, match="explicit-only"):
        resolve_model("brains/auto", classification, providers_factory=factory)

    explicit = resolve_model("github_copilot/gpt-5", classification, providers_factory=factory)
    assert explicit["resolution"] == "direct"
    assert explicit["provider_name"] == "github_copilot"
    assert explicit["model"] == "gpt-5"

    tier_alias = resolve_model("brains/default", classification, providers_factory=factory)
    assert tier_alias["resolution"] == "alias"
    assert tier_alias["provider_name"] == "github_copilot"


def test_resolve_tier_aliases_work_with_classifier_disallowed(_classification):
    """Tier aliases other than ``brains/auto`` are explicit client
    choices — they must keep working even with the classifier off."""
    route = resolve_model(
        "brains/cheap",
        _classification,
        providers_factory=_fake_factory(),
        allow_classifier=False,
    )
    assert route["tier"] == "cheap"
    assert route["resolution"] == "alias"


@pytest.mark.parametrize(
    "alias,expected_tier",
    [
        ("sonnet", "default"),
        ("opus", "strong"),
        ("haiku", "cheap"),
        ("fable", "deep"),
        ("fable-5", "deep"),
        ("best", "deep"),
        ("claude-sonnet-4.5", "default"),
        ("claude-sonnet-4-5[1m]", "default"),
        ("claude-opus-4.5", "strong"),
        ("claude-haiku-3", "cheap"),
        ("claude-fable-5", "deep"),
    ],
)
def test_resolve_anthropic_cli_aliases(_router_enabled, _classification, alias, expected_tier):
    """CLI shorthand only fires when step 2 (catalog match) hasn't
    already claimed the id. With an empty stub catalog these all fall
    through to step 3 → tier mapping."""
    route = resolve_model(alias, _classification, providers_factory=_fake_factory())
    assert route["tier"] == expected_tier
    assert route["resolution"] == "tier"


# ----- direct provider catalog match -------------------------------------


def test_resolve_direct_provider_match(_router_enabled, _classification, monkeypatch):
    """When a provider's catalog exposes the requested id, route there
    verbatim and tag the resolution as ``direct``."""
    fake_provider = SimpleNamespace(
        list_models=lambda: [
            {"id": "gpt-4o-mini", "vendor": "openai", "label": "GPT-4o mini"},
            {"id": "gpt-4o", "vendor": "openai", "label": "GPT-4o"},
        ]
    )
    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="openai_compatible", model="gpt-4o"),
    )

    def factory(name: str):
        assert name == "openai_compatible"
        return fake_provider

    route = resolve_model("gpt-4o-mini", _classification, providers_factory=factory)
    assert route["resolution"] == "direct"
    assert route["provider_name"] == "openai_compatible"
    assert route["model"] == "gpt-4o-mini"  # verbatim from catalog


def test_resolve_provider_namespaced_id_pins_provider(
    _router_enabled, _classification, monkeypatch
):
    """``<provider>/<model>`` form pins both — even if another provider
    also exposes ``model`` in its catalog."""
    openai_provider = SimpleNamespace(list_models=lambda: [{"id": "gpt-4o", "vendor": "openai"}])
    copilot_provider = SimpleNamespace(list_models=lambda: [{"id": "gpt-4o", "vendor": "github"}])

    monkeypatch.setitem(
        settings.models, "cheap", ModelRoute(provider="openai_compatible", model="gpt-4o")
    )
    monkeypatch.setitem(
        settings.models, "default", ModelRoute(provider="github_copilot", model="gpt-4o")
    )

    def factory(name: str):
        if name == "openai_compatible":
            return openai_provider
        if name == "github_copilot":
            return copilot_provider
        raise AssertionError(f"unexpected provider {name!r}")

    route = resolve_model("github_copilot/gpt-4o", _classification, providers_factory=factory)
    assert route["resolution"] == "direct"
    assert route["provider_name"] == "github_copilot"
    assert route["model"] == "gpt-4o"


def test_resolve_direct_match_beats_anthropic_cli_alias(
    _router_enabled, _classification, monkeypatch
):
    """Regression: ``claude-sonnet-4.6`` is a real catalog id in
    production. The OLD resolver matched the Anthropic CLI shorthand
    step first (``startswith("claude-sonnet")``) and silently
    downgraded the request. NEW order puts the direct catalog match
    first."""
    fake_provider = SimpleNamespace(
        list_models=lambda: [{"id": "claude-sonnet-4.6", "vendor": "anthropic"}]
    )
    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="github_copilot", model="claude-sonnet-4.6"),
    )

    def factory(name: str):
        return fake_provider

    route = resolve_model("claude-sonnet-4.6", _classification, providers_factory=factory)
    assert route["resolution"] == "direct"
    assert route["model"] == "claude-sonnet-4.6"  # verbatim, no silent downgrade


def test_resolve_direct_match_handles_punctuation_variants(
    _router_enabled, _classification, monkeypatch
):
    """``claude-sonnet-4.5`` and ``claude-sonnet-4-5`` must hit the
    same catalog entry — and the resolver returns the catalog's
    *original* id on the wire (not the normalised one)."""
    fake_provider = SimpleNamespace(
        list_models=lambda: [{"id": "claude-sonnet-4.5", "vendor": "anthropic"}]
    )
    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="github_copilot", model="claude-sonnet-4.5"),
    )

    def factory(name: str):
        return fake_provider

    route = resolve_model("claude-sonnet-4-5", _classification, providers_factory=factory)
    assert route["resolution"] == "direct"
    # Verbatim catalog id makes it onto the wire, not the normalised form.
    assert route["model"] == "claude-sonnet-4.5"


# ----- not found ---------------------------------------------------------


def test_resolve_unknown_raises_model_not_found(_router_enabled, _classification):
    """Unknown id with routing on → ``ModelNotFoundError``. No silent
    classifier fallback: the client always learns about an unknown id
    instead of having its prompt sent to a different model."""
    with pytest.raises(ModelNotFoundError) as exc:
        resolve_model(
            "totally-unknown-model-xyz",
            _classification,
            providers_factory=_fake_factory(),
        )
    assert exc.value.requested == "totally-unknown-model-xyz"


def test_resolve_empty_raises_model_not_found(_router_enabled, _classification):
    with pytest.raises(ModelNotFoundError):
        resolve_model("", _classification, providers_factory=_fake_factory())


def test_resolve_none_raises_model_not_found(_router_enabled, _classification):
    with pytest.raises(ModelNotFoundError):
        resolve_model(None, _classification, providers_factory=_fake_factory())


# ----- never raises for catalog edge cases -------------------------------


def test_resolve_swallows_provider_factory_errors(_router_enabled, _classification):
    """If a provider can't be constructed during catalog scan, skip it
    — don't crash. The eventual outcome for an unknown name is
    :class:`ModelNotFoundError`, not the provider's exception."""

    def broken_factory(name: str):
        raise RuntimeError("provider blew up")

    # Tier-alias path constructs the tier's provider (broken) — same
    # surfacing behaviour as the legacy router for misconfigured tiers.
    with pytest.raises(RuntimeError):
        resolve_model("brains-default", _classification, providers_factory=broken_factory)

    # Unknown-name path: the catalog scan swallows the factory error,
    # then step 4 raises ModelNotFoundError. RuntimeError must NOT leak.
    with pytest.raises(ModelNotFoundError):
        resolve_model("xyz-unknown", _classification, providers_factory=broken_factory)


def test_resolve_swallows_list_models_errors(_router_enabled, _classification, monkeypatch):
    """A provider whose ``list_models()`` raises mid-call contributes
    zero rows — never propagates. Unknown name → ModelNotFoundError."""

    class Boom:
        def list_models(self):
            raise RuntimeError("catalog upstream down")

    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="openai_compatible", model="gpt-4o"),
    )

    def factory(name: str):
        return Boom()

    with pytest.raises(ModelNotFoundError):
        resolve_model("not-in-catalog", _classification, providers_factory=factory)


def test_resolve_with_real_get_provider_default_config(_router_enabled, _classification):
    """End-to-end: default config (echo tiers) + real get_provider —
    known names resolve cleanly, unknown names raise (no silent fallback)."""
    for name in (
        "brains-auto",
        "brains-cheap",
        "claude-brains-strong",
        "sonnet",
        "echo-default",
    ):
        route = resolve_model(name, _classification)
        assert "provider" in route and route["model"]

    for name in ("unknown-xyz", "", None):
        with pytest.raises(ModelNotFoundError):
            resolve_model(name, _classification)


# ----- integration with select_model wrapper -----------------------------


def test_select_model_router_enabled_passes_known_through(monkeypatch):
    """Known id with routing on → direct catalog match, verbatim on
    the wire, no classifier consulted."""
    from brains.router.model_router import select_model

    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))
    classification = classify([{"role": "user", "content": "hello"}])
    route = select_model(classification, requested_model="echo-default")
    assert route["model"] == "echo-default"
    assert route["resolution"] == "direct"


def test_select_model_router_disabled_refuses_brains_auto(_router_disabled):
    """Routing off → ``brains/auto`` is refused; the explicit
    classifier-driven alias is the one path the flag actually gates."""
    from brains.router.model_router import select_model

    classification = classify([{"role": "user", "content": "anything"}])
    with pytest.raises(ModelNotFoundError):
        select_model(classification, requested_model="brains/auto")
    with pytest.raises(ModelNotFoundError):
        select_model(classification, requested_model="brains-auto")


def test_select_model_router_disabled_honours_tier_aliases(_router_disabled):
    """Tier aliases other than ``brains/auto`` are explicit client
    choices and keep working with routing off."""
    from brains.router.model_router import select_model

    classification = classify([{"role": "user", "content": "anything"}])
    route = select_model(classification, requested_model="brains-strong")
    assert route["tier"] == "strong"


def test_select_model_router_enabled_routes_via_resolver(monkeypatch):
    """Router-enabled path delegates to the resolver."""
    from brains.router.model_router import select_model

    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))
    classification = classify([{"role": "user", "content": "anything"}])
    route = select_model(classification, requested_model="brains-strong")
    assert route["tier"] == "strong"
    assert route["model"] == settings.models["strong"].model


def test_select_model_unknown_raises_model_not_found(monkeypatch):
    """Unknown name → ``ModelNotFoundError`` regardless of the flag."""
    from brains.router.model_router import select_model

    monkeypatch.setattr(settings, "router", RouterConfig(enabled=True))
    classification = classify([{"role": "user", "content": "fix the failing test"}])
    with pytest.raises(ModelNotFoundError):
        select_model(classification, requested_model="unknown-xyz")


# ----- ensure resolver does NOT swallow ProviderConfigError on the
# tier-alias path so 500s still surface when an alias points at a
# provider that can't be constructed (legacy behaviour).


def test_resolve_tier_alias_propagates_provider_config_error(_router_enabled, _classification):
    from brains.providers.registry import ProviderConfigError

    def factory(name: str):
        raise ProviderConfigError(f"unknown provider {name!r}")

    with pytest.raises(ProviderConfigError):
        resolve_model("brains-default", _classification, providers_factory=factory)


# ----- defensive: malformed catalog rows are ignored, not crashy --------


def test_resolve_skips_malformed_catalog_rows(_router_enabled, _classification, monkeypatch):
    monkeypatch.setitem(
        settings.models,
        "default",
        ModelRoute(provider="openai_compatible", model="gpt-4o"),
    )
    fake = SimpleNamespace(
        list_models=lambda: [
            "not a dict",
            {"id": ""},
            {"vendor": "no-id-here"},
            {"id": "gpt-4o"},
        ]
    )
    route = resolve_model("gpt-4o", _classification, providers_factory=lambda _n: fake)
    assert route["resolution"] == "direct"
    assert route["model"] == "gpt-4o"


# ----- ensure patching get_provider isn't required for normal usage -----


def test_resolve_uses_default_factory_when_not_provided(_router_enabled, _classification):
    """Default ``providers_factory`` is :func:`get_provider`. Verify a
    resolver call without an explicit factory still works on the
    default (echo) config."""
    route = resolve_model("echo-default", _classification)
    assert route["resolution"] == "direct"
    assert route["model"] == "echo-default"
