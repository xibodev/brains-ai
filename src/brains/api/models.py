"""OpenAI-shaped ``/v1/models`` endpoint.

Returns a union of:

- **Brains tier aliases** in both dialects (so OpenAI clients *and*
  Claude Code's claude-prefixed discovery filter see them):
  ``brains-auto`` / ``brains-cheap`` / ``brains-default`` /
  ``brains-strong`` / ``brains-deep`` and the matching
  ``claude-brains-*`` variants.
- **Provider catalog entries** from every registered non-stub provider
  (independent of tier wiring), namespaced as ``<provider>/<model_id>``
  so a configured provider's real model ids are discoverable and
  directly usable. ``list_models()`` failures (transport, parsing,
  auth, anything) are swallowed per provider so a single broken or
  unconfigured catalog never 500s the endpoint.

The response shape matches the OpenAI spec — ``{object: "list", data:
[{id, object, owned_by, ...}]}`` — so it's a drop-in for any client
that already knows ``GET /v1/models``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from brains.api.auth import require_api_key
from brains.config import settings
from brains.providers.registry import available_providers, get_provider, is_stub_provider

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


# Tier alias rows. Slash form is canonical (preferred in new clients +
# matches the ``provider/model`` namespacing scheme). Hyphen form stays
# present for backwards compatibility — both forms route to the same
# tier via ``router.resolver._TIER_ALIASES``. ``brains/auto`` only
# resolves when ``settings.router.enabled = True``; we list it
# conditionally below.
_BRAINS_TIER_ALIASES_FIXED: tuple[tuple[str, str], ...] = (
    ("brains/cheap", "cheap"),
    ("brains/default", "default"),
    ("brains/strong", "strong"),
    ("brains/deep", "deep"),
    # Legacy hyphen form (drop in a future major bump).
    ("brains-cheap", "cheap"),
    ("brains-default", "default"),
    ("brains-strong", "strong"),
    ("brains-deep", "deep"),
    # claude- prefixed (Claude Code's model picker filters for
    # ``claude*``/``anthropic*`` ids, so we expose under that prefix
    # too — same underlying tiers).
    ("claude-brains-cheap", "cheap"),
    ("claude-brains-default", "default"),
    ("claude-brains-strong", "strong"),
    ("claude-brains-deep", "deep"),
)


def _tier_display(tier: str | None) -> str:
    # Public-surface display name. Deliberately does NOT echo the
    # provider+model that the operator pinned to this tier — that's
    # internal wiring and only belongs on the auth-gated
    # ``/admin/api/config`` view. Clients picking a tier alias don't
    # need to know which backend serves it; they just need to know
    # the alias exists and what tier it maps to.
    if tier is None:
        return "Brains — auto (classifier picks the tier)"
    return f"Brains — {tier} tier"


def _real_provider_names() -> list[str]:
    """Every registered NON-stub provider, regardless of tier wiring.

    A provider you've configured is usable directly via its real
    ``<provider>/<model>`` ids whether or not any tier points at it, so
    its catalog belongs in ``/v1/models``. Unconfigured / unreachable
    providers simply contribute zero rows (``list_models()`` failures are
    swallowed in :func:`_safe_provider_catalog`). Stubs (``echo``) are
    skipped — they're dev scaffolding, not real model sources.
    """
    return [name for name in available_providers() if not is_stub_provider(name)]


def _safe_provider_catalog(provider_name: str) -> list[dict]:
    """Resolve a provider and list its models without ever raising.

    Catalog discovery is best-effort: if the provider can't be
    constructed (missing extra, missing creds) or its
    ``list_models()`` raises mid-call, we just contribute zero rows
    for that provider and let the rest of the endpoint succeed.
    """
    try:
        provider = get_provider(provider_name)
    except Exception:  # noqa: BLE001 — endpoint must never 500
        return []
    try:
        rows = provider.list_models() or []
    except Exception:  # noqa: BLE001 — see above
        return []
    if not isinstance(rows, list):
        return []
    cleaned: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.strip():
            continue
        cleaned.append(row)
    return cleaned


@router.get("/models")
def models():
    """OpenAI-compatible model catalog used by Claude Code, the OpenAI
    SDK ``models.list()`` call, ``gh copilot``'s gateway discovery,
    and any other client doing ``GET /v1/models``.

    Composition:

    - ``brains/auto`` — listed only when ``router.enabled = True``,
      because it's the one alias that depends on the classifier.
    - ``brains/<tier>`` and hyphen-form siblings — always listed; they
      resolve directly to operator-pinned models and don't involve the
      classifier.
    - ``<provider>/<model_id>`` — every model from every connected
      provider's catalog, namespaced by provider so two providers can
      surface overlapping ids without colliding. The catalog row's
      original id is preserved as the suffix so a faithful proxy
      request can use it verbatim against the upstream API.
    """
    data: list[dict] = []
    seen_ids: set[str] = set()

    # 1. Brains tier aliases (slash + historical hyphen + claude-prefixed).
    if settings.router.enabled:
        # ``brains/auto`` is gated — listing it when routing is off
        # would advertise a feature that 404s.
        for alias_id in ("brains/auto", "brains-auto"):
            data.append(
                {
                    "id": alias_id,
                    "object": "model",
                    "owned_by": "brains",
                    "display_name": _tier_display(None),
                }
            )
            seen_ids.add(alias_id)
    for alias_id, tier in _BRAINS_TIER_ALIASES_FIXED:
        data.append(
            {
                "id": alias_id,
                "object": "model",
                "owned_by": "brains",
                "display_name": _tier_display(tier),
            }
        )
        seen_ids.add(alias_id)

    # 2. Provider catalogs, each id namespaced as ``<provider>/<id>``.
    # Every CONFIGURED real provider's models are listed here —
    # independent of tier wiring — so you can discover and call a real
    # model id directly (``github_copilot/claude-sonnet-4.5``) without
    # going through a tier. Bare ids would collide across providers, so
    # we namespace; the original upstream id is preserved as the suffix
    # for a faithful proxy request.
    for provider_name in _real_provider_names():
        for row in _safe_provider_catalog(provider_name):
            row_id = row["id"]
            namespaced_id = f"{provider_name}/{row_id}"
            if namespaced_id in seen_ids:
                continue
            seen_ids.add(namespaced_id)
            entry: dict = {
                "id": namespaced_id,
                "object": "model",
                "owned_by": provider_name,
            }
            label = row.get("label") or row.get("name")
            if isinstance(label, str) and label.strip():
                entry["display_name"] = label
            vendor = row.get("vendor")
            if isinstance(vendor, str) and vendor.strip():
                entry["vendor"] = vendor
            # Pass through provider-reported capabilities (e.g. the
            # ``reasoning_effort`` thinking levels a model accepts, context
            # window, feature flags) so a client can discover them without a
            # second, provider-specific call.
            caps = row.get("capabilities")
            if isinstance(caps, dict) and caps:
                entry["capabilities"] = caps
            data.append(entry)

    return {"object": "list", "data": data}
