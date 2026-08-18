"""Model-name resolver.

Maps any incoming model string (from any dialect — OpenAI-shaped,
Anthropic-shaped, raw provider id, or a brains tier alias) to a
concrete ``(provider, model, tier)`` tuple. Three-step lookup:

  1. **Brains tier alias** → use that tier's configured ``(provider,
     model)`` from ``settings.models``.
     Recognised: ``brains-auto`` (router decides), ``brains-cheap``,
     ``brains-default``, ``brains-strong``, ``brains-deep``. Also the
     Anthropic-flavoured siblings ``claude-brains-cheap``,
     ``claude-brains-default``, ``claude-brains-strong``,
     ``claude-brains-deep`` — Claude Code's gateway discovery filters
     ``/v1/models`` for ``claude*``/``anthropic*`` ids, so we expose
     the same tiers under both prefixes.
  2. **Anthropic CLI alias** → map to closest brains tier:
       ``fable``/``fable-5``/``claude-fable-5``    → ``deep``
       ``opus``/``claude-opus*``                   → ``strong``
       ``sonnet``/``claude-sonnet*``               → ``default``
       ``haiku``/``claude-haiku*``                 → ``cheap``
       ``best``                                    → ``deep``
       ``default``                                 → ``default``
  3. **Direct provider-model match** → if any enabled provider's
     ``list_models()`` exposes an id that normalises to the same key
     as the requested name, use that ``(provider, model)`` verbatim.
  4. **Fallback** → classifier-driven tier (legacy behaviour).

Normalisation rules — applied to **both sides** before every
comparison:

  - lower-case + strip whitespace
  - strip ``[1m]``, ``[200k]``, ``[100k]`` etc. context-window markers
  - strip ``(...)`` parenthetical suffixes
  - collapse runs of ``-`` or ``.`` to a single ``-`` so
    ``claude-sonnet-4.5`` and ``claude-sonnet-4-5`` match the same key

The resolver is **pure** in the sense that it never raises for an
unknown name — it falls through to the classifier-driven fallback.
The provider catalog stays authoritative for the upstream model
string actually sent on the wire: when a direct match is found, we
return the catalog's original id, not the normalised key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from brains.config import settings
from brains.providers.registry import ProviderConfigError, get_provider, is_stub_provider

logger = logging.getLogger(__name__)


# --- normalisation -------------------------------------------------------

_CONTEXT_WINDOW_SUFFIX = re.compile(r"\[\s*\d+[kmKM]\s*\]")
_PARENTHETICAL_SUFFIX = re.compile(r"\([^)]*\)")
_PUNCT_RUN = re.compile(r"[-.\s]+")


def normalize_model_name(name: str | None) -> str:
    """Canonicalise a model identifier for lookup.

    Returns an empty string for ``None`` / empty / whitespace-only
    input. Never raises.
    """
    if not name:
        return ""
    text = str(name).strip().lower()
    text = _CONTEXT_WINDOW_SUFFIX.sub("", text)
    text = _PARENTHETICAL_SUFFIX.sub("", text)
    text = text.strip()
    text = _PUNCT_RUN.sub("-", text)
    return text.strip("-")


# --- tier alias tables ---------------------------------------------------

# Brains tier aliases. Both slash form (canonical, going forward) and
# hyphen form (legacy, accepted as alias) plus the ``claude-``-prefixed
# variants Claude Code's gateway discovery filter needs to surface.
# Maps the normalised alias → tier name in ``settings.models``.
# ``brains/auto`` (and ``brains-auto``) resolves to ``None`` so the
# caller falls through to the classifier.
_TIER_ALIASES: dict[str, str | None] = {
    # slash form — canonical
    "brains/auto": None,
    "brains/cheap": "cheap",
    "brains/default": "default",
    "brains/strong": "strong",
    "brains/deep": "deep",
    # hyphen form — legacy, kept for back-compat
    "brains-auto": None,
    "brains-cheap": "cheap",
    "brains-default": "default",
    "brains-strong": "strong",
    "brains-deep": "deep",
    # claude- prefixed (Claude Code gateway discovery only surfaces
    # ``claude*``/``anthropic*`` ids in its model picker).
    "claude-brains-cheap": "cheap",
    "claude-brains-default": "default",
    "claude-brains-strong": "strong",
    "claude-brains-deep": "deep",
}


# Public sentinel raised by ``resolve_model`` when running in
# pure-proxy mode and no provider catalog claims the requested model.
class ModelNotFoundError(LookupError):
    """Raised by :func:`resolve_model` when ``allow_fallbacks=False``
    and the requested model isn't a tier alias and isn't found in any
    connected provider's catalog. Callers (typically the API handlers)
    convert this into a 404 so the client knows it asked for something
    brains can't proxy."""

    def __init__(self, requested: str | None, message: str | None = None) -> None:
        self.requested = requested or ""
        super().__init__(
            message
            or f"Model {self.requested!r} is not a brains tier alias and is not "
            f"exposed by any connected provider's catalog. "
            f"Either pick a model from GET /v1/models, or enable tier routing "
            f"(settings.router.enabled = true) to use brains/auto + tier aliases."
        )


def _anthropic_cli_alias_tier(normalised: str) -> str | None:
    """Map an Anthropic CLI-flavoured alias to a brains tier.

    Returns ``None`` when the input doesn't look like an Anthropic
    family alias. Pattern matches the public ``claude-*`` family names
    plus the bare shorthands (``sonnet``, ``opus``, ``haiku``,
    ``fable``, ``best``, ``default``).
    """
    if not normalised:
        return None
    # Exact short-form aliases first.
    short = {
        "fable": "deep",
        "fable-5": "deep",
        "best": "deep",
        "opus": "strong",
        "sonnet": "default",
        "haiku": "cheap",
        "default": "default",
    }
    if normalised in short:
        return short[normalised]
    # ``claude-fable-5`` / ``claude-opus-*`` / etc.
    if normalised.startswith("claude-fable"):
        return "deep"
    if normalised.startswith("claude-opus"):
        return "strong"
    if normalised.startswith("claude-sonnet"):
        return "default"
    if normalised.startswith("claude-haiku"):
        return "cheap"
    return None


# --- enabled-provider helpers -------------------------------------------


def _enabled_provider_names() -> list[str]:
    """Provider names referenced by any configured tier in settings.

    Deduplicated while preserving the tier order from
    ``settings.models`` so the resolver checks the operator-preferred
    provider first when several catalogs include the same id.
    """
    seen: set[str] = set()
    names: list[str] = []
    for route in settings.models.values():
        name = route.provider
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _safe_list_models(provider: Any) -> list[dict[str, Any]]:
    """``provider.list_models()`` that never raises.

    Returns ``[]`` on any exception — provider catalog discovery is
    advisory; a flaky upstream must never break request routing.
    """
    try:
        rows = provider.list_models() or []
    except Exception:  # noqa: BLE001 — catalog is advisory; never fatal
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


# --- public API ---------------------------------------------------------


def _route_for_tier(
    tier: str,
    *,
    providers_factory: Callable[[str], Any],
    resolution: str,
) -> dict[str, Any]:
    route = settings.models.get(tier) or settings.models.get(
        "default", next(iter(settings.models.values()))
    )
    provider = providers_factory(route.provider)
    return {
        "provider": provider,
        "provider_name": route.provider,
        "model": route.model,
        "tier": tier if tier in settings.models else "default",
        "resolution": resolution,
        "is_stub": is_stub_provider(route.provider),
    }


def _classifier_fallback(
    classification: Any,
    *,
    providers_factory: Callable[[str], Any],
    requested: str | None = None,
) -> dict[str, Any]:
    tier = settings.routes.get(
        getattr(classification, "task_type", "unknown"),
        getattr(classification, "recommended_model_tier", "default"),
    )
    route = settings.models.get(tier, settings.models["default"])
    if route.provider == "github_copilot":
        raise ModelNotFoundError(
            requested,
            "github_copilot is explicit-only and not selectable via brains/auto; "
            "address it directly as github_copilot/<model>",
        )
    provider = providers_factory(route.provider)
    return {
        "provider": provider,
        "provider_name": route.provider,
        "model": route.model,
        "tier": tier,
        "resolution": "fallback",
        "is_stub": is_stub_provider(route.provider),
    }


def resolve_model(
    requested: str | None,
    classification: Any,
    providers_factory: Callable[[str], Any] = get_provider,
    *,
    allow_classifier: bool = True,
) -> dict[str, Any]:
    """Resolve a requested model name to a concrete ``(provider, model, tier)``.

    Step order (first match wins):

    1. **Brains tier alias** (``brains/auto``, ``brains/cheap``, hyphen
       siblings, ``claude-brains-*``) → resolves to the operator-pinned
       tier. The special ``brains/auto`` is the ONLY path that runs
       the classifier; it requires ``allow_classifier=True``.
    2. **Direct provider catalog match** — including the
       ``provider/model`` form (e.g. ``github_copilot/claude-sonnet-4.6``)
       which pins both the provider AND the model. Bare ids are matched
       against every enabled provider's catalog; first one wins.
    3. **Anthropic CLI shorthand** (``sonnet``/``opus``/``haiku``/
       ``fable``/``best`` and the bare ``default``) → tier mapping.
       Real ``claude-<family>-<version>`` ids are NOT auto-mapped here:
       they get a chance to hit the catalog at step 2 first, which
       prevents the silent ``claude-sonnet-4.6 → default tier``
       downgrade.
    4. **Not found** → raises :class:`ModelNotFoundError`. We never
       silently classify a request the client didn't explicitly route
       to ``brains/auto``. If the request used an explicit model id
       brains can't honour, the client should learn that immediately
       rather than have its prompt sent to a different model than it
       picked.

    Parameters
    ----------
    requested:
        Model string the client sent.
    classification:
        Classifier output. Only consulted for the explicit
        ``brains/auto`` path.
    providers_factory:
        Indirection so tests can inject a fake. Defaults to
        :func:`brains.providers.registry.get_provider`.
    allow_classifier:
        When ``False`` (i.e. ``settings.router.enabled == False``),
        ``brains/auto`` is refused with :class:`ModelNotFoundError`
        too. Tier aliases and direct matches still work because
        they're explicit client choices not requiring inference.

    Raises
    ------
    ModelNotFoundError
        Requested name is not a tier alias, not in any catalog, and
        not an Anthropic CLI shorthand (or is ``brains/auto`` with
        ``allow_classifier=False``).
    ProviderConfigError
        Bubbles up from :func:`get_provider`; the caller converts to
        500 like the legacy router did.
    """
    normalised = normalize_model_name(requested)

    # 1. Tier alias (slash + hyphen + claude-prefixed).
    if normalised in _TIER_ALIASES:
        tier = _TIER_ALIASES[normalised]
        if tier is None:
            # ``brains/auto`` — classifier-driven. Gated on the
            # routing-enabled flag because it's the one path that lets
            # brains pick the model on the client's behalf.
            if not allow_classifier:
                raise ModelNotFoundError(requested)
            return _classifier_fallback(
                classification,
                providers_factory=providers_factory,
                requested=requested,
            ) | {"resolution": "alias"}
        return _route_for_tier(tier, providers_factory=providers_factory, resolution="alias")

    # 2. Direct provider-model match (including provider/model form).
    if normalised:
        provider_hint: str | None = None
        model_part = normalised
        if "/" in normalised:
            head, _, tail = normalised.partition("/")
            if head and tail:
                provider_hint = head
                model_part = tail

        candidate_providers: list[str]
        candidate_providers = [provider_hint] if provider_hint else _enabled_provider_names()

        for provider_name in candidate_providers:
            try:
                provider = providers_factory(provider_name)
            except ProviderConfigError:
                continue
            except Exception:  # noqa: BLE001 — defensive, never block routing
                continue
            for row in _safe_list_models(provider):
                row_id = row.get("id")
                if not isinstance(row_id, str):
                    continue
                if normalize_model_name(row_id) == model_part:
                    tier_label = _tier_for_provider(provider_name)
                    return {
                        "provider": provider,
                        "provider_name": provider_name,
                        "model": row_id,
                        "tier": tier_label,
                        "resolution": "direct",
                        "is_stub": is_stub_provider(provider_name),
                    }

    # 3. Anthropic CLI shorthand (sonnet/opus/haiku/fable/best/default).
    tier = _anthropic_cli_alias_tier(normalised)
    if tier is not None:
        return _route_for_tier(tier, providers_factory=providers_factory, resolution="tier")

    # 4. Not found — no silent classifier fallback. Force the client
    # to either pick a real model or explicitly opt into brains/auto.
    raise ModelNotFoundError(requested)


def _tier_for_provider(provider_name: str) -> str:
    """Best-effort label for the tier whose route uses ``provider_name``.

    Used to tag direct-match routes so the savings ledger has
    something to bucket on. Falls back to ``"default"`` when nothing
    matches, which matches the legacy code path's tier label.
    """
    for tier, route in settings.models.items():
        if route.provider == provider_name:
            return tier
    return "default"


__all__ = ["normalize_model_name", "resolve_model"]
