"""Admin service layer: overlay persistence + provider connection tests."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from brains.config import (
    ADMIN_EDITABLE_KEYS,
    ENV_REF_ALLOWED_FIELDS,
    ControlPolicy,
    ModelRoute,
    RouterConfig,
    SavingsConfig,
    _looks_like_env_ref,
    reload_settings,
    settings,
)
from brains.providers.base import Provider
from brains.providers.registry import (
    ProviderConfigError,
    ProviderInvocationError,
    available_providers,
    get_provider,
)

# Fields the admin form serializes through. The overlay layer enforces the
# allowlist via ``ADMIN_EDITABLE_KEYS`` so we just pass values through.
RUNTIME_OVERLAY_HEADER = (
    "# Brains runtime overlay.\n"
    "# Managed by the admin UI. Hand-edits are merged on top of BRAINS_CONFIG.\n"
    "# Secrets MUST use ${ENV:NAME} references; they are resolved at load time.\n"
)


# Overlay keys whose value is a provider base URL. Each one goes through
# ``_validate_provider_base_url`` before persistence so the admin form
# can't redirect outbound traffic at link-local metadata services or
# (unless explicitly enabled) at private LAN ranges.
_BASE_URL_FIELDS: frozenset[str] = frozenset(
    {
        "ollama_base_url",
        "openai_compatible_base_url",
    }
)


def _is_private_v4(host: ipaddress.IPv4Address) -> bool:
    return host.is_private and not host.is_loopback and not host.is_link_local


def _is_private_v6(host: ipaddress.IPv6Address) -> bool:
    return host.is_private and not host.is_loopback and not host.is_link_local


def _validate_provider_base_url(field: str, value: Any) -> str:
    """Reject base URLs likely to enable SSRF before they hit the overlay.

    Allow rules:

    - Scheme must be ``http`` or ``https``.
    - Loopback (``127.0.0.0/8``, ``::1``) is always allowed — that's how
      a local-first user points at Ollama or a local OpenAI proxy.
    - Link-local addresses (``169.254.0.0/16``, ``fe80::/10``) are
      always rejected. This is the cloud-metadata SSRF vector.
    - Private RFC1918 ranges (``10/8``, ``172.16/12``, ``192.168/16``)
      and ULA IPv6 are rejected by default. Set
      ``BRAINS_ALLOW_PRIVATE_PROVIDERS=1`` if you really run an LLM
      gateway on your LAN.
    - DNS names and public IPs are accepted (OpenAI, Anthropic, etc.).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: base URL must be a non-empty string")
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field}: only http/https URLs are permitted (got {parsed.scheme!r})")
    if not parsed.hostname:
        raise ValueError(f"{field}: URL must include a hostname")
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP — treat as DNS name. We don't resolve here.
        return candidate
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError(f"{field}: link-local / metadata / reserved addresses are not permitted")
    if ip.is_loopback:
        return candidate
    private = _is_private_v4(ip) if isinstance(ip, ipaddress.IPv4Address) else _is_private_v6(ip)
    if private and os.environ.get("BRAINS_ALLOW_PRIVATE_PROVIDERS", "") != "1":
        raise ValueError(
            f"{field}: private network addresses are blocked; "
            "set BRAINS_ALLOW_PRIVATE_PROVIDERS=1 to opt in"
        )
    return candidate


def _reject_env_ref_outside_allowlist(field_path: str, value: Any) -> None:
    """Refuse ``${ENV:NAME}`` syntax in fields that aren't on the allowlist."""
    top_level = field_path.split(".", 1)[0]
    if top_level in ENV_REF_ALLOWED_FIELDS:
        return
    if isinstance(value, str) and _looks_like_env_ref(value):
        raise ValueError(f"{field_path}: ${{ENV:NAME}} references are not permitted in this field")
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_env_ref_outside_allowlist(f"{field_path}.{k}", v)
    elif isinstance(value, list | tuple):
        for idx, v in enumerate(value):
            _reject_env_ref_outside_allowlist(f"{field_path}[{idx}]", v)


def _overlay_path() -> Path:
    raw = settings.runtime_overlay or "brains.runtime.yaml"
    return Path(raw)


def read_overlay() -> dict[str, Any]:
    """Return the current overlay contents as a dict (empty if missing)."""
    path = _overlay_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def write_overlay(updates: dict[str, Any], reload: bool = True) -> dict[str, Any]:
    """Merge ``updates`` into the overlay and persist.

    Keys outside ``ADMIN_EDITABLE_KEYS`` are dropped so the admin UI
    cannot mutate sensitive defaults like ``api_key`` or ``db_url``.
    Provider base URLs are validated to block SSRF vectors and
    ``${ENV:NAME}`` references are rejected on every field that isn't
    on ``ENV_REF_ALLOWED_FIELDS``.

    ``reload`` controls whether we re-run ``load_settings()`` after the
    write. The admin UI wants the reload (so subsequent requests see the
    new config) and gets it by default. The ``brains-ai features`` wizard
    passes ``reload=False`` because (a) it's about to exit and (b)
    enabling a subsystem before its extra is installed would trip the
    startup gate and abort the write the operator just authorised. The
    gate still fires correctly the next time the server starts.
    """
    current = read_overlay()
    for key, value in updates.items():
        if key not in ADMIN_EDITABLE_KEYS:
            continue
        if value is None:
            current.pop(key, None)
            continue
        _reject_env_ref_outside_allowlist(key, value)
        if key in _BASE_URL_FIELDS:
            value = _validate_provider_base_url(key, value)
        current[key] = value
    path = _overlay_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(RUNTIME_OVERLAY_HEADER)
        yaml.safe_dump(current, handle, sort_keys=True, default_flow_style=False)
    if reload:
        reload_settings()
    return current


def current_config_view() -> dict[str, Any]:
    """A redacted snapshot of the live settings for the admin UI."""
    return {
        "models": {
            tier: {"provider": route.provider, "model": route.model}
            for tier, route in settings.models.items()
        },
        "routes": dict(settings.routes),
        "control": settings.control.model_dump(),
        "router": settings.router.model_dump(),
        "savings": settings.savings.model_dump(),
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "ollama_base_url": settings.ollama_base_url,
        "ollama_timeout_seconds": settings.ollama_timeout_seconds,
        "openai_compatible_base_url": settings.openai_compatible_base_url,
        "openai_compatible_api_key": (
            settings.openai_compatible_api_key
            if str(settings.openai_compatible_api_key or "").startswith("${ENV:")
            else ("***set***" if settings.openai_compatible_api_key else "")
        ),
        "openai_compatible_api_key_set": bool(settings.openai_compatible_api_key),
        "openai_compatible_timeout_seconds": settings.openai_compatible_timeout_seconds,
        "litellm_timeout_seconds": settings.litellm_timeout_seconds,
        "github_copilot_use_gh_cli": settings.github_copilot_use_gh_cli,
        "github_copilot_cache_dir": settings.github_copilot_cache_dir,
        "github_copilot_timeout_seconds": settings.github_copilot_timeout_seconds,
        "github_copilot_editor_version": settings.github_copilot_editor_version,
        "github_copilot_integration_id": settings.github_copilot_integration_id,
        "allow_copilot_proxy": settings.allow_copilot_proxy,
        "gateway_preamble": settings.gateway_preamble,
        "embed_model": settings.embed_model,
        "context_compression_enabled": settings.context_compression_enabled,
        "savings_holdout_fraction": settings.savings_holdout_fraction,
        "source_allowlist": list(settings.source_allowlist),
        "api_key_set": bool(settings.api_key),
        "api_keys_count": len(settings.api_keys),
        "allow_unauthenticated_api": settings.allow_unauthenticated_api,
    }


def known_env_names() -> list[str]:
    """Environment variable names currently referenced by the overlay."""
    overlay = read_overlay()
    names: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            if value.startswith("${ENV:") and value.endswith("}"):
                names.add(value[len("${ENV:") : -1])
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    _walk(overlay)
    return sorted(names)


# Canonical list of every env var brains reads, with category + purpose.
# Used by the Secrets page to render a checklist of "what you can set"
# rather than only "what's referenced". Keep this in sync with config.py
# and the provider modules — the smoke test asserts the columns it cares
# about most are still present.
KNOWN_ENV_CATALOG: list[dict[str, str]] = [
    {
        "name": "BRAINS_API_KEY",
        "category": "auth",
        "required": "recommended",
        "purpose": "Single bearer key the gateway accepts. Set this OR BRAINS_API_KEYS.",
    },
    {
        "name": "BRAINS_API_KEYS",
        "category": "auth",
        "required": "optional",
        "purpose": "Comma-separated list of bearer keys for key rotation.",
    },
    {
        "name": "BRAINS_ADMIN_KEY",
        "category": "auth",
        "required": "recommended",
        "purpose": "Separate key for the admin console + admin API endpoints.",
    },
    {
        "name": "BRAINS_ALLOW_UNAUTHENTICATED",
        "category": "auth",
        "required": "off by default",
        "purpose": "Set to 1 only on a sealed loopback box. Disables ALL auth checks.",
    },
    {
        "name": "BRAINS_DB_URL",
        "category": "storage",
        "required": "optional",
        "purpose": "SQLAlchemy URL. Defaults to sqlite:///~/.brains/brains.db",
    },
    {
        "name": "BRAINS_CONFIG",
        "category": "config",
        "required": "optional",
        "purpose": "Path to the base YAML (overlay is merged on top).",
    },
    {
        "name": "BRAINS_RUNTIME_OVERLAY",
        "category": "config",
        "required": "optional",
        "purpose": "Path to the admin-managed overlay YAML. Defaults to ./brains.runtime.yaml",
    },
    {
        "name": "BRAINS_GITHUB_COPILOT_OAUTH_TOKEN",
        "category": "github_copilot",
        "required": "optional",
        "purpose": "Skip gh CLI lookup and use this OAuth token directly. Auto-discovered from gh CLI if not set.",
    },
    {
        "name": "OPENAI_API_KEY",
        "category": "openai_compatible",
        "required": "optional",
        "purpose": "Common default for openai_compatible.api_key — referenced via ${ENV:OPENAI_API_KEY} in the overlay.",
    },
    {
        "name": "BRAINS_ALLOW_PRIVATE_PROVIDERS",
        "category": "network",
        "required": "off by default",
        "purpose": "Set to 1 to let admin point providers at RFC1918 LAN addresses. Loopback is always allowed.",
    },
    {
        "name": "GH_TOKEN",
        "category": "github_copilot",
        "required": "optional",
        "purpose": "Used by the gh CLI itself. Brains reads gh's token store, not this var directly.",
    },
]


def env_catalog_with_status() -> list[dict[str, Any]]:
    """Return KNOWN_ENV_CATALOG plus a ``set`` boolean per row."""
    return [{**entry, "set": entry["name"] in os.environ} for entry in KNOWN_ENV_CATALOG]


def env_var_status(names: list[str]) -> dict[str, bool]:
    return {name: name in os.environ for name in names}


# ---------------------------------------------------------------------------
# Environment override (admin Environment page)
#
# Lets the operator supply a value for an env var brains reads, applied
# either to the live process only (ephemeral) or also persisted to a local
# gitignored secrets file that brains loads on every start. Secrets are
# never written to the repo or the YAML overlay; the persist target lives
# under the per-machine state dir.
# ---------------------------------------------------------------------------

_SECRETS_HEADER = (
    "# Brains local secrets — gitignored, merged into the environment at startup.\n"
    "# Managed by the admin Environment page. Real env vars take precedence.\n"
    "# Do not commit this file.\n"
)


def env_override_allowed_names() -> set[str]:
    """Env var names the override UI may set: every name brains documents in
    the catalog plus any name the active overlay already references. This
    bounds the surface so the admin form can't inject arbitrary vars."""
    names = {entry["name"] for entry in KNOWN_ENV_CATALOG}
    names.update(known_env_names())
    return names


def _read_secrets_env() -> dict[str, str]:
    from brains.config import secrets_env_path

    path = secrets_env_path()
    data: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip()
    return data


def _write_secrets_env(data: dict[str, str]) -> None:
    from brains.config import secrets_env_path

    path = secrets_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_SECRETS_HEADER.rstrip("\n")]
    for key in sorted(data):
        lines.append(f"{key}={data[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def set_env_override(name: str, value: str, *, persist: bool) -> dict[str, Any]:
    """Apply an env value to the live process (and optionally persist it),
    then reload settings so ``${ENV:NAME}`` references resolve immediately."""
    os.environ[name] = value
    persisted = False
    if persist:
        data = _read_secrets_env()
        data[name] = value
        _write_secrets_env(data)
        persisted = True
    reload_settings()
    return {"ok": True, "name": name, "set": name in os.environ, "persisted": persisted}


def unset_env_override(name: str, *, persist: bool) -> dict[str, Any]:
    """Remove an override from the live process and, when ``persist`` is set,
    from the local secrets file too, then reload settings."""
    os.environ.pop(name, None)
    removed_file = False
    if persist:
        data = _read_secrets_env()
        if name in data:
            del data[name]
            _write_secrets_env(data)
            removed_file = True
    reload_settings()
    return {"ok": True, "name": name, "set": name in os.environ, "removed_file": removed_file}


def test_provider_connection(
    provider_name: str,
    model: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Attempt a tiny completion against ``provider_name`` and report result.

    Returns ``{ok, latency_ms, error?, response_preview?}``.
    """
    started = time.monotonic()
    try:
        provider: Provider = get_provider(provider_name)
    except ProviderConfigError as exc:
        return {
            "ok": False,
            "latency_ms": 0,
            "error": str(exc),
            "stage": "configuration",
        }
    try:
        response = provider.complete(
            model,
            [{"role": "user", "content": "ping"}],
            max_tokens=8,
        )
    except ProviderInvocationError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
            "stage": "invocation",
        }
    except Exception as exc:  # noqa: BLE001 - surface anything the provider throws
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "stage": "invocation",
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    # Do NOT reflect upstream bytes back to the admin UI. A successful
    # call returns a fixed ``ok`` token plus a sha256 fingerprint of the
    # first 200 bytes of the response so an operator has a stable
    # diagnostic without exposing the upstream body (potential SSRF
    # exfiltration channel pre-FIX-001).
    fingerprint = ""
    try:
        body = str(response["choices"][0]["message"]["content"])[:200]
        fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    except (KeyError, IndexError, TypeError):
        fingerprint = ""
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "response": "ok",
        "response_fingerprint": fingerprint,
        "stage": "ok",
    }


def list_provider_choices() -> list[str]:
    return sorted(available_providers())


def provider_status_view() -> list[dict[str, Any]]:
    """Cheap, settings-derived status of every registered provider.

    Returns one dict per provider with:
      * ``name`` — provider id (``echo``, ``openai_compatible``, ...).
      * ``is_stub`` — ``True`` for built-in dev stubs (``echo``).
      * ``configured`` — ``True`` when the provider has enough settings
        to attempt a request. For stubs this is always ``True``. For
        real providers it's a settings-only check (no upstream
        connection), so this is safe to call on every page load.
      * ``reason`` — short human-readable explanation when
        ``configured`` is ``False`` (``""`` otherwise). Used by the
        admin UI to show "Not configured — missing base_url" without
        revealing secrets.

    Live connectivity testing remains a button-driven action (see
    :func:`test_provider_connection`) — this function intentionally
    never opens a socket.
    """
    from brains.providers.registry import is_stub_provider

    out: list[dict[str, Any]] = []
    for name in sorted(available_providers()):
        is_stub = is_stub_provider(name)
        if is_stub:
            out.append(
                {
                    "name": name,
                    "is_stub": True,
                    "configured": True,
                    "reason": "stub provider — no upstream required",
                }
            )
            continue
        configured, reason = _provider_settings_status(name)
        out.append(
            {
                "name": name,
                "is_stub": False,
                "configured": configured,
                "reason": reason,
            }
        )
    return out


def _provider_settings_status(name: str) -> tuple[bool, str]:
    """Return ``(configured, reason)`` for a real (non-stub) provider
    based purely on :data:`settings`. ``reason`` is empty when
    configured."""
    if name == "ollama":
        # Ollama is configured by default (loopback URL); even if the
        # local daemon isn't running, the URL itself is always set so
        # the provider can attempt a request. Reachability is a
        # button-driven test, not a page-load badge.
        return True, ""
    if name in ("openai", "openai_compatible"):
        base = (settings.openai_compatible_base_url or "").strip()
        if not base:
            return False, "missing openai_compatible_base_url"
        if "api.openai.com" in base.lower() and not settings.openai_compatible_api_key:
            return False, "OpenAI endpoint requires an API key"
        return True, ""
    if name == "litellm":
        # LiteLLM has its own per-model config; treat it as configured
        # whenever the extra is installed.
        try:
            import litellm  # noqa: F401  (presence check)

            return True, ""
        except Exception:
            return False, "litellm extra not installed (pip install 'brains-ai[litellm]')"
    if name == "github_copilot":
        # Either gh CLI fallback is enabled OR an explicit OAuth token
        # is wired through env. We don't open a socket here.
        if settings.github_copilot_use_gh_cli:
            return True, ""
        if os.environ.get("BRAINS_GITHUB_COPILOT_OAUTH_TOKEN"):
            return True, ""
        return False, "no gh CLI fallback and BRAINS_GITHUB_COPILOT_OAUTH_TOKEN unset"
    # Unknown provider: optimistic default. If the registry exposed it,
    # assume the operator knows what they're doing.
    return True, ""


def price_catalog_view() -> dict[str, Any]:
    """Merged static price catalog + overlay overrides.

    Returns ``{prices: {model_id: {input, output}}, overlay_count: N}``
    where ``prices`` is the longest-prefix-friendly lookup table the
    admin UI uses to annotate model dropdowns with their per-1M-token
    cost. ``input`` / ``output`` are USD per 1M tokens.
    """
    from brains.router.prices import DEFAULT_PRICES

    merged: dict[str, dict[str, float]] = {
        model: {"input": float(inp), "output": float(out)}
        for model, (inp, out) in DEFAULT_PRICES.items()
    }
    overlay = settings.savings.price_catalog or {}
    overlay_count = 0
    for model, entry in overlay.items():
        if not isinstance(entry, dict):
            continue
        try:
            inp = float(cast("float", entry.get("input")))
            out = float(cast("float", entry.get("output")))
        except (TypeError, ValueError):
            continue
        merged[str(model)] = {"input": inp, "output": out}
        overlay_count += 1
    return {"prices": merged, "overlay_count": overlay_count}


# Canonical route keys the classifier emits. The map is *advisory* —
# operators can still register custom keys via the admin UI, the
# classifier's ``task_type`` is free-form. The list backs the admin
# Routes editor's autocomplete suggestions so common keys don't have
# to be typed from memory or copied out of the classifier source.
_CANONICAL_ROUTE_KEYS: tuple[tuple[str, str], ...] = (
    ("code_fix", "fix / patch / failing test requests"),
    ("code_explanation", "explain / why / how questions about code"),
    ("architecture", "system design, tradeoffs, scalability discussions"),
    ("docs_lookup", "version / API / changelog / latest-release lookups"),
    ("research", "compare / survey / benchmark tasks"),
    ("unknown", "fallback when no rule scores high enough"),
)


def route_keys_view() -> dict[str, Any]:
    """Catalog of canonical route keys + advisory descriptions.

    Used by the admin Routes editor to drive the route-key input
    autocomplete. Free-form route keys still work — this is purely a
    convenience listing of the keys the classifier currently emits.
    """
    return {"keys": [{"key": key, "description": desc} for key, desc in _CANONICAL_ROUTE_KEYS]}


def savings_preview_view(
    *,
    model: str,
    days: int = 7,
    current_model: str | None = None,
) -> dict[str, Any]:
    """Project what the savings ledger would look like if *model* were
    used in place of *current_model* over the trailing *days* window.

    Re-costs every non-stub ledger row whose ``routed_model`` equals
    *current_model* (or every row if *current_model* is ``None``) using
    *model* as the projected cost, while keeping each row's existing
    baseline cost. The response includes the current actual cost (sum
    of the matched rows untouched) and the projected actual cost (sum
    after re-pricing) so the UI can show "if you switch to
    gpt-4o-mini, your last 7d would have cost $X instead of $Y" at the
    point of choice — without writing anything.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from brains.router.prices import lookup_price
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import UsageLedgerEntry

    init_db()
    since = datetime.now(UTC) - timedelta(days=max(1, int(days)))
    overlay = settings.savings.price_catalog or {}

    projected_price = lookup_price(model, overrides=overlay)
    with SessionLocal() as session:
        filters = [
            UsageLedgerEntry.ts >= since,
            UsageLedgerEntry.is_stub.is_(False),
        ]
        if current_model:
            filters.append(UsageLedgerEntry.routed_model == current_model)
        rows = list(session.execute(select(UsageLedgerEntry).where(*filters)).scalars())

    rows_considered = len(rows)
    matched_rows = 0
    current_actual = 0.0
    projected_actual = 0.0
    baseline = 0.0
    input_tokens = 0
    output_tokens = 0
    for row in rows:
        current_actual += float(row.cost_actual_usd or 0.0)
        baseline += float(row.cost_baseline_usd or 0.0)
        input_tokens += int(row.input_tokens or 0)
        output_tokens += int(row.output_tokens or 0)
        if projected_price is None:
            # Unknown projected model -> we can't price it; mirror current.
            projected_actual += float(row.cost_actual_usd or 0.0)
            continue
        matched_rows += 1
        inp_per_m, out_per_m = projected_price
        cost = (int(row.input_tokens or 0) / 1_000_000.0) * float(inp_per_m) + (
            int(row.output_tokens or 0) / 1_000_000.0
        ) * float(out_per_m)
        projected_actual += cost

    return {
        "window_days": int(days),
        "current_model": current_model,
        "projected_model": model,
        "projected_price_per_million": (
            {"input": projected_price[0], "output": projected_price[1]} if projected_price else None
        ),
        "rows_considered": rows_considered,
        "rows_repriced": matched_rows,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "current_actual_usd": round(current_actual, 6),
        "projected_actual_usd": round(projected_actual, 6),
        "baseline_usd": round(baseline, 6),
        "delta_usd": round(current_actual - projected_actual, 6),
    }


def list_provider_models(provider_name: str) -> dict[str, Any]:
    """Best-effort discovery of the models a provider exposes.

    Returns ``{"provider": name, "models": [...], "error": str | None}``.
    Wraps ``Provider.list_models()`` so callers get a uniform envelope
    regardless of whether the upstream is reachable, configured, or even
    installed (litellm with the extra missing, etc.). The endpoint is
    intentionally non-mutating and safe to call from the admin UI on
    every provider-dropdown change.
    """
    try:
        provider: Provider = get_provider(provider_name)
    except ProviderConfigError as exc:
        return {"provider": provider_name, "models": [], "error": str(exc)}
    try:
        models = provider.list_models() or []
    except ProviderInvocationError as exc:
        return {"provider": provider_name, "models": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never crash the dropdown
        return {
            "provider": provider_name,
            "models": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    # Coerce/clean each row so the UI can render uniformly without trusting
    # any provider to return well-formed dicts.
    cleaned: list[dict[str, Any]] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        cleaned.append(
            {
                "id": model_id.strip(),
                "vendor": str(entry["vendor"]).strip() if entry.get("vendor") else None,
                "label": str(entry["label"]).strip() if entry.get("label") else None,
            }
        )
    return {"provider": provider_name, "models": cleaned, "error": None}


# --- github_copilot dashboard auth (device-code flow) -------------------


def copilot_auth_status() -> dict[str, Any]:
    """OAuth + gate snapshot for the github_copilot provider card.

    Reports which token source is active, whether the proxy gate is
    enabled (``allow_copilot_proxy``), and whether it is currently
    permitted at all (the gate also refuses on a shared Postgres backend
    or with multiple operators). Never returns token material.
    """
    from brains.auth.copilot import CopilotAuthError, assert_copilot_proxy_allowed, auth_status

    status = dict(auth_status())
    status["proxy_enabled"] = bool(settings.allow_copilot_proxy)
    try:
        assert_copilot_proxy_allowed()
        status["proxy_allowed"] = True
        status["proxy_blocked_reason"] = ""
    except CopilotAuthError as exc:
        status["proxy_allowed"] = False
        status["proxy_blocked_reason"] = str(exc)
    return status


def copilot_device_start() -> dict[str, Any]:
    """Begin the GitHub device-code flow.

    Returns ``{user_code, verification_uri, device_code, interval,
    expires_in}`` for the dashboard to display, or ``{"error": str}`` on a
    transport failure. The operator visits the URL, enters the code, and
    the dashboard polls :func:`copilot_device_poll` until authorized.
    """
    from brains.auth.copilot import CopilotAuthError, start_device_flow

    try:
        device = start_device_flow()
    except CopilotAuthError as exc:
        return {"error": str(exc)}
    return {
        "device_code": device.device_code,
        "user_code": device.user_code,
        "verification_uri": device.verification_uri,
        "interval": device.interval,
        "expires_in": device.expires_in,
    }


def copilot_device_poll(device_code: str) -> dict[str, Any]:
    """One non-blocking poll of the device-code flow. The dashboard calls
    this on its own timer until ``status`` leaves ``pending``/``slow_down``.
    On ``authorized`` the OAuth token is cached server-side."""
    from brains.auth.copilot import poll_device_flow_once

    return poll_device_flow_once(device_code)


def copilot_logout() -> dict[str, Any]:
    """Delete cached github_copilot OAuth + session tokens."""
    from brains.auth.copilot import clear_cached_credentials

    return {"removed": clear_cached_credentials()}


def serialize_models(models: dict[str, ModelRoute]) -> dict[str, dict[str, str]]:
    return {
        tier: {"provider": route.provider, "model": route.model} for tier, route in models.items()
    }


def parse_models_payload(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Validate and normalize a models payload sent from the admin form."""
    if not isinstance(payload, dict):
        raise ValueError("models payload must be a mapping")
    _reject_env_ref_outside_allowlist("models", payload)
    normalized: dict[str, dict[str, str]] = {}
    for tier, entry in payload.items():
        if not isinstance(entry, dict):
            raise ValueError(f"models.{tier} must be a mapping")
        provider = entry.get("provider")
        model = entry.get("model")
        if not provider or not model:
            raise ValueError(f"models.{tier} requires both provider and model")
        # Validate against the known provider set so admin can't save a typo.
        if provider not in available_providers():
            raise ValueError(f"unknown provider for tier {tier}: {provider}")
        normalized[tier] = {"provider": str(provider), "model": str(model)}
    return normalized


def parse_routes_payload(payload: dict[str, Any], known_tiers: set[str]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("routes payload must be a mapping")
    _reject_env_ref_outside_allowlist("routes", payload)
    normalized: dict[str, str] = {}
    for task_type, tier in payload.items():
        if tier not in known_tiers:
            raise ValueError(f"routes.{task_type}: tier '{tier}' is not defined in models")
        normalized[str(task_type)] = str(tier)
    return normalized


def parse_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("control payload must be a mapping")
    _reject_env_ref_outside_allowlist("control", payload)
    # Validate by round-tripping through pydantic.
    return ControlPolicy.model_validate(payload).model_dump()


def parse_router_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the ``router`` overlay block (currently the on/off toggle).

    Round-trips through :class:`RouterConfig` so additional fields can be
    added later without changing the admin write path.
    """
    if not isinstance(payload, dict):
        raise ValueError("router payload must be a mapping")
    _reject_env_ref_outside_allowlist("router", payload)
    return RouterConfig.model_validate(payload).model_dump()


def parse_savings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the ``savings`` overlay block.

    Currently accepts ``enabled`` (bool), ``baseline_model`` (str), and
    ``price_catalog`` (mapping of ``model -> {input, output}``). Each
    catalog entry's prices are coerced to floats; non-numeric values are
    silently rejected by the :class:`SavingsConfig` validator below.
    """
    if not isinstance(payload, dict):
        raise ValueError("savings payload must be a mapping")
    _reject_env_ref_outside_allowlist("savings", payload)
    # Light sanity check on the catalog shape before pydantic validation
    # so callers get a friendlier error message than "Input should be a
    # valid dictionary".
    catalog = payload.get("price_catalog")
    if catalog is not None and not isinstance(catalog, dict):
        raise ValueError("savings.price_catalog must be a mapping")
    if isinstance(catalog, dict):
        for model_id, spec in catalog.items():
            if not isinstance(spec, dict):
                raise ValueError(
                    f"savings.price_catalog['{model_id}'] must be a mapping"
                    " with 'input' and 'output' keys"
                )
    return SavingsConfig.model_validate(payload).model_dump()
