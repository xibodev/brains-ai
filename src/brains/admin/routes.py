"""Admin FastAPI router.

Exposes a JSON API under ``/admin/api/*`` and a small HTML console under
``/admin/*``. Both surfaces edit **install-level** configuration - provider
credentials, environment overrides, router policy, the process config itself -
none of which is Org-attributed. They are therefore gated by
``require_install_admin`` (JSON) and ``require_install_admin_html`` (HTML,
which redirects to the login form instead of returning a bare 401): the
bootstrap admin holding the install's own key, and nobody else. An Org
``owner`` is refused, because owning an Org is not owning the install.

Only ``/admin/login`` and ``/admin/logout`` are outside that gate - they are
how an operator obtains the console cookie in the first place.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from brains.admin.service import (
    copilot_auth_status,
    copilot_device_poll,
    copilot_device_start,
    copilot_logout,
    current_config_view,
    env_catalog_with_status,
    env_override_allowed_names,
    env_var_status,
    known_env_names,
    list_provider_choices,
    list_provider_models,
    parse_control_payload,
    parse_models_payload,
    parse_router_payload,
    parse_routes_payload,
    parse_savings_payload,
    price_catalog_view,
    provider_status_view,
    read_overlay,
    route_keys_view,
    savings_preview_view,
    set_env_override,
    test_provider_connection,
    unset_env_override,
    write_overlay,
)
from brains.admin.ui import (
    ADMIN_NAV,
)
from brains.api.auth import (
    BROWSER_AUTH_COOKIE,
    BROWSER_COOKIE_TTL_SECONDS,
    _matches_any_key,
    _valid_keys,
    mint_browser_token,
    require_install_admin,
    require_install_admin_html,
)
from brains.audit import record as audit_record
from brains.audit import required_effect as audit_required_effect
from brains.config import settings
from brains.web import render_response

router = APIRouter()


def _console_principal_for(key: str):
    """Resolve a raw key to a principal allowed to hold a console cookie.

    A key that still sits in ``_valid_keys()`` but whose credential row has
    been revoked must not be able to mint a console cookie, so the login form
    resolves the credential rather than only matching the raw string. Two
    principals are refused outright, matching the console gate itself: a
    Runtime credential, and a principal with no Org role at all.
    """
    from brains.authz.principal import Principal
    from brains.authz.resolver import principal_for_secret

    try:
        principal = principal_for_secret(key)
    except Exception:
        return None
    if not isinstance(principal, Principal):
        return None
    if principal.is_runtime or not principal.has_any_org_role:
        return None
    return principal


_ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


# ---------- JSON API ----------


@router.get("/admin/api/providers", dependencies=[Depends(require_install_admin)])
def api_providers() -> dict[str, Any]:
    return {
        "providers": list_provider_choices(),
        "current": current_config_view(),
    }


@router.get("/admin/api/providers/status", dependencies=[Depends(require_install_admin)])
def api_providers_status() -> dict[str, Any]:
    """Settings-derived per-provider status — cheap, no upstream probe.

    Backs the connection badges on each Provider card in the admin
    Config page. Live "Test connection" remains a separate explicit
    button (``/admin/api/providers/test``).
    """
    return {"providers": provider_status_view()}


@router.get("/admin/api/prices", dependencies=[Depends(require_install_admin)])
def api_prices() -> dict[str, Any]:
    """Merged static price catalog + overlay overrides.

    Used by the admin UI to annotate the per-tier model dropdown with
    `$in/$out per 1M` price hints so operators can see the savings
    advantage at the point of choice.
    """
    return price_catalog_view()


@router.get("/admin/api/route-keys", dependencies=[Depends(require_install_admin)])
def api_route_keys() -> dict[str, Any]:
    """Canonical route keys the classifier emits, with one-line
    descriptions. Powers the admin Routes editor autocomplete.
    """
    return route_keys_view()


@router.get("/admin/api/savings/preview", dependencies=[Depends(require_install_admin)])
def api_savings_preview(
    model: str,
    days: int = 7,
    current_model: str | None = None,
) -> dict[str, Any]:
    """Project the savings ledger if ``model`` were used in place of
    ``current_model`` over the trailing ``days`` window. Pure
    read-only — does not mutate the ledger.
    """
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    return savings_preview_view(model=model, days=days, current_model=current_model)


@router.post("/admin/api/providers/test", dependencies=[Depends(require_install_admin)])
def api_test_provider(body: dict[str, Any]) -> dict[str, Any]:
    provider = body.get("provider")
    model = body.get("model")
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    return test_provider_connection(str(provider), str(model))


@router.get(
    "/admin/api/providers/{name}/models",
    dependencies=[Depends(require_install_admin)],
)
def api_provider_models(name: str) -> dict[str, Any]:
    """Best-effort enumeration of a provider's available models.

    Powers the Test Connection model dropdown so operators don't have to
    memorise model slugs. Always returns 200 with a structured envelope
    (``models`` + ``error``) — never raises on upstream failure, since the
    UI degrades gracefully to free-text input.
    """
    return list_provider_models(name)


@router.get(
    "/admin/api/providers/github_copilot/auth-status",
    dependencies=[Depends(require_install_admin)],
)
def api_copilot_auth_status() -> dict[str, Any]:
    """OAuth + gate snapshot for the github_copilot provider card.

    Reports the active token source, whether the proxy gate is enabled,
    and whether it is currently permitted. Never returns token material.
    """
    return copilot_auth_status()


@router.post(
    "/admin/api/providers/github_copilot/device/start",
    dependencies=[Depends(require_install_admin)],
)
def api_copilot_device_start() -> dict[str, Any]:
    """Begin the GitHub device-code flow for the github_copilot provider.

    Returns the user code + verification URL for the operator to visit,
    or an ``error`` envelope on transport failure.
    """
    result = copilot_device_start()
    audit_record(
        actor="admin",
        action="provider.copilot_device_start",
        payload={"ok": "error" not in result},
    )
    return result


@router.post(
    "/admin/api/providers/github_copilot/device/poll",
    dependencies=[Depends(require_install_admin)],
)
def api_copilot_device_poll(body: dict[str, Any]) -> dict[str, Any]:
    """One non-blocking poll of the device-code flow (driven by the UI's
    timer). On ``authorized`` the OAuth token is cached server-side."""
    device_code = str(body.get("device_code") or "").strip()
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code is required")
    result = copilot_device_poll(device_code)
    if result.get("status") == "authorized":
        audit_record(
            actor="admin",
            action="provider.copilot_device_authorized",
            payload={},
        )
    return result


@router.post(
    "/admin/api/providers/github_copilot/logout",
    dependencies=[Depends(require_install_admin)],
)
def api_copilot_logout() -> dict[str, Any]:
    """Delete cached github_copilot OAuth + session tokens."""
    result = copilot_logout()
    audit_record(actor="admin", action="provider.copilot_logout", payload={})
    return result


@router.get("/admin/api/config", dependencies=[Depends(require_install_admin)])
def api_get_config() -> dict[str, Any]:
    return {
        "live": current_config_view(),
        "overlay": read_overlay(),
        "overlay_path": settings.runtime_overlay,
    }


@router.post("/admin/api/config", dependencies=[Depends(require_install_admin)])
def api_post_config(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist admin edits.

    The body is a partial overlay; only keys in ``ADMIN_EDITABLE_KEYS`` are
    written. Models are validated against the registered provider set so a
    typo can't break the next request.
    """
    try:
        updates: dict[str, Any] = {}
        if "models" in body:
            updates["models"] = parse_models_payload(body["models"])
        known_tiers = set((updates.get("models") or current_config_view()["models"]).keys())
        if "routes" in body:
            updates["routes"] = parse_routes_payload(body["routes"], known_tiers)
        if "control" in body:
            updates["control"] = parse_control_payload(body["control"])
        if "router" in body:
            updates["router"] = parse_router_payload(body["router"])
        if "savings" in body:
            updates["savings"] = parse_savings_payload(body["savings"])
        for scalar in (
            "rate_limit_per_minute",
            "ollama_base_url",
            "ollama_timeout_seconds",
            "openai_compatible_base_url",
            "openai_compatible_api_key",
            "openai_compatible_timeout_seconds",
            "litellm_timeout_seconds",
            "source_allowlist",
            "context_compression_enabled",
            "savings_holdout_fraction",
            "github_copilot_use_gh_cli",
            "github_copilot_cache_dir",
            "github_copilot_timeout_seconds",
            "github_copilot_editor_version",
            "github_copilot_integration_id",
            "allow_copilot_proxy",
            "gateway_preamble",
            "embed_model",
        ):
            if scalar in body:
                updates[scalar] = body[scalar]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        # The overlay is a file, so the record cannot share its transaction:
        # the attempt is committed first and the write is only reported once
        # it returned. Recording after the write would raise on a store that
        # is down, but the config would already have changed.
        with audit_required_effect(
            actor="admin",
            action="admin.overlay_write",
            payload={"keys": sorted(updates.keys())},
        ):
            new_overlay = write_overlay(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"overlay": new_overlay, "live": current_config_view()}


@router.get("/admin/api/env", dependencies=[Depends(require_install_admin)])
def api_env_status() -> dict[str, Any]:
    names = known_env_names()
    return {"names": names, "set": env_var_status(names)}


@router.post("/admin/api/env/check", dependencies=[Depends(require_install_admin)])
def api_env_check(body: dict[str, Any]) -> dict[str, bool]:
    names = body.get("names") or []
    if not isinstance(names, list):
        raise HTTPException(status_code=400, detail="names must be a list")
    requested = [str(n) for n in names]
    allowed = set(known_env_names())
    unknown = [n for n in requested if n not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(f"env names must already be referenced by the overlay; unknown: {unknown}"),
        )
    return env_var_status(requested)


def _validated_override_name(body: dict[str, Any]) -> str:
    name = str(body.get("name") or "").strip()
    if not _ENV_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail="env name must match ^[A-Z][A-Z0-9_]*$",
        )
    if name not in env_override_allowed_names():
        raise HTTPException(
            status_code=400,
            detail=(
                f"{name} is not an env var brains reads; only catalog names or "
                "overlay-referenced names may be overridden"
            ),
        )
    return name


@router.post("/admin/api/env/set", dependencies=[Depends(require_install_admin)])
def api_env_set(body: dict[str, Any]) -> dict[str, Any]:
    """Set a value for a known env var on the live process, optionally
    persisting it to the local gitignored secrets file. The raw value is
    never logged or written to the YAML overlay."""
    name = _validated_override_name(body)
    value = body.get("value")
    if not isinstance(value, str) or value == "":
        raise HTTPException(status_code=400, detail="value must be a non-empty string")
    persist = bool(body.get("persist"))
    with audit_required_effect(
        actor="admin",
        action="admin.env_override_set",
        payload={"name": name, "persisted": persist},
    ):
        result = set_env_override(name, value, persist=persist)
    return result


@router.post("/admin/api/env/clear", dependencies=[Depends(require_install_admin)])
def api_env_clear(body: dict[str, Any]) -> dict[str, Any]:
    """Remove an env override from the live process (and the secrets file
    when ``persist`` is set)."""
    name = _validated_override_name(body)
    persist = bool(body.get("persist"))
    with audit_required_effect(
        actor="admin",
        action="admin.env_override_clear",
        payload={"name": name, "persisted": persist},
    ):
        result = unset_env_override(name, persist=persist)
    return result


@router.get("/admin/api/savings", dependencies=[Depends(require_install_admin)])
def api_savings(days: int = 7, include_stubs: int = 0) -> dict[str, Any]:
    """Read-side view of the usage ledger: totals + per-day series + top
    routed models over the trailing *days* window. Backs the
    dashboard's savings panel.

    Stub-provider rows (e.g. ``echo``) are excluded by default so the
    headline reflects real upstream traffic. Pass ``include_stubs=1``
    to fold them back in (useful for dev smoke verification).
    """
    from brains.config import settings as _settings
    from brains.providers.registry import stub_provider_names
    from brains.router.savings import daily_series, savings_summary, top_routed_models, totals

    window = max(1, min(int(days or 7), 90))
    include = bool(int(include_stubs or 0))
    return {
        "enabled": bool(_settings.savings.enabled),
        "baseline_model": _settings.savings.baseline_model or "",
        "stub_providers": stub_provider_names(),
        "include_stubs": include,
        "totals": totals(window, include_stubs=include),
        "summary": savings_summary(window),
        "daily": daily_series(window, include_stubs=include),
        "top_routed_models": top_routed_models(window, include_stubs=include),
    }


# ---------- HTML console ----------


@router.get("/admin", response_class=HTMLResponse)
def admin_root(request: Request):  # noqa: D401
    # If the visitor already has a valid cookie, drop them on the
    # overview; otherwise send them straight to the login page (instead
    # of bouncing through a 401).
    cookie = request.cookies.get(BROWSER_AUTH_COOKIE)
    if cookie:
        return RedirectResponse(url="/admin/overview", status_code=303)
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(error: str | None = None, next: str | None = None):  # noqa: A002
    return render_response(
        "admin/login.html",
        page_title="Sign in",
        error=error,
        next_url=next,
    )


@router.post("/admin/login")
def admin_login(
    request: Request,
    key: str = Form(...),
    next: str | None = Form(default=None),  # noqa: A002
):
    if not _matches_any_key(key, _valid_keys()) or _console_principal_for(key) is None:
        from urllib.parse import quote

        suffix = f"&next={quote(next, safe='')}" if next else ""
        return RedirectResponse(url=f"/admin/login?error=Invalid+key{suffix}", status_code=303)
    # Only allow same-origin relative redirects so a malicious caller
    # can't smuggle an off-site target via the next= parameter.
    target = (
        next if (next and next.startswith("/") and not next.startswith("//")) else "/admin/overview"
    )
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        BROWSER_AUTH_COOKIE,
        mint_browser_token(key),
        httponly=True,
        samesite="strict",
        secure=(request.url.scheme == "https"),
        max_age=BROWSER_COOKIE_TTL_SECONDS,
    )
    return response


@router.get("/admin/logout")
def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(BROWSER_AUTH_COOKIE)
    return response


@router.get(
    "/admin/overview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def admin_overview_page():
    config = current_config_view()
    providers = list_provider_choices()
    from brains.providers.registry import stub_provider_names

    stub_providers = set(stub_provider_names())
    overview = {
        "greeting": "Control plane",
        "tagline": (
            "Local-first gateway routing AI coding agents across model tiers, "
            "with overlay-managed configuration and end-to-end auth."
        ),
        "providers": providers,
        "stub_providers": sorted(stub_providers),
        "tier_count": len(config["models"]),
        "route_count": len(config["routes"]),
        "models": config["models"],
        "routes": config["routes"],
        "rate_limit_per_minute": config["rate_limit_per_minute"],
        "api_key_set": config["api_key_set"],
        "api_keys_count": config["api_keys_count"],
        "openai_compatible_api_key_set": config["openai_compatible_api_key_set"],
        "allow_unauthenticated_api": config["allow_unauthenticated_api"],
        "overlay_path": settings.runtime_overlay,
        "router_enabled": bool(config.get("router", {}).get("enabled", True)),
    }
    # Headline savings number — best-effort. If the ledger is empty (or
    # disabled, or errors out for any reason) we just hide the panel.
    savings_block: dict[str, Any] | None = None
    if settings.savings.enabled:
        try:
            from brains.router.savings import (
                daily_series,
                savings_summary,
                top_routed_models,
                totals,
            )

            t = totals(days=7)
            savings_block = {
                "totals": t,
                "summary": savings_summary(days=7),
                "daily": daily_series(days=7),
                "top": top_routed_models(days=7, limit=3),
                "baseline_model": settings.savings.baseline_model
                or (settings.models["default"].model if "default" in settings.models else ""),
            }
        except Exception:
            savings_block = None
    return render_response(
        "admin/overview.html",
        page_title="Overview",
        active="overview",
        admin_nav=ADMIN_NAV,
        overview=overview,
        savings=savings_block,
    )


@router.get(
    "/admin/config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def admin_config_page(saved: int = 0, error: str | None = None):
    config = current_config_view()
    overlay = read_overlay()
    providers = list_provider_choices()
    from brains.providers.registry import stub_provider_names

    stub_providers = set(stub_provider_names())
    bootstrap = {
        "providers": providers,
        "models": config["models"],
        "routes": config["routes"],
        "provider_config": {
            "ollama": {
                "base_url": config.get("ollama_base_url", ""),
                "timeout_seconds": config.get("ollama_timeout_seconds", 60),
            },
            "openai_compatible": {
                "base_url": config.get("openai_compatible_base_url", ""),
                "api_key": config.get("openai_compatible_api_key", ""),
                "api_key_set": config.get("openai_compatible_api_key_set", False),
                "timeout_seconds": config.get("openai_compatible_timeout_seconds", 60),
            },
            "github_copilot": {
                "use_gh_cli": config.get("github_copilot_use_gh_cli", True),
                "cache_dir": config.get("github_copilot_cache_dir", ""),
                "timeout_seconds": config.get("github_copilot_timeout_seconds", 60),
                "editor_version": config.get("github_copilot_editor_version", "vscode/1.95.3"),
                "integration_id": config.get("github_copilot_integration_id", "vscode-chat"),
                "allow_copilot_proxy": config.get("allow_copilot_proxy", False),
            },
            "litellm": {
                "timeout_seconds": config.get("litellm_timeout_seconds", 60),
            },
        },
        "rate_limit_per_minute": config.get("rate_limit_per_minute", 0),
    }
    return render_response(
        "admin/config.html",
        page_title="Config",
        active="config",
        admin_nav=ADMIN_NAV,
        config=config,
        providers=providers,
        stub_providers=sorted(stub_providers),
        bootstrap_json=json.dumps(bootstrap),
        models_json_default=json.dumps(config["models"], indent=2),
        routes_json_default=json.dumps(config["routes"], indent=2),
        overlay_json=json.dumps(overlay, indent=2) if overlay else "{}",
        saved=bool(saved),
        error=error,
    )


@router.post("/admin/config", dependencies=[Depends(require_install_admin_html)])
def admin_config_save(
    models_json: str = Form(""),
    routes_json: str = Form(""),
    rate_limit_per_minute: int = Form(0),
    ollama_base_url: str = Form(""),
    openai_compatible_base_url: str = Form(""),
    openai_compatible_api_key_ref: str = Form(""),
):
    updates: dict[str, Any] = {}
    try:
        if models_json.strip():
            updates["models"] = parse_models_payload(json.loads(models_json))
        known_tiers = set((updates.get("models") or current_config_view()["models"]).keys())
        if routes_json.strip():
            updates["routes"] = parse_routes_payload(json.loads(routes_json), known_tiers)
    except (json.JSONDecodeError, ValueError) as exc:
        return RedirectResponse(url=f"/admin/config?error={str(exc)[:160]}", status_code=303)
    updates["rate_limit_per_minute"] = int(rate_limit_per_minute)
    if ollama_base_url:
        updates["ollama_base_url"] = ollama_base_url
    if openai_compatible_base_url:
        updates["openai_compatible_base_url"] = openai_compatible_base_url
    # The API key field accepts a bare env-var name; we wrap it in the
    # ${ENV:NAME} ref so the secret stays in the environment.
    ref = openai_compatible_api_key_ref.strip()
    if ref:
        if ref.startswith("${ENV:") and ref.endswith("}"):
            inner = ref[len("${ENV:") : -1]
            if not _ENV_NAME_PATTERN.match(inner):
                return RedirectResponse(
                    url="/admin/config?error=invalid+env+var+name",
                    status_code=303,
                )
            updates["openai_compatible_api_key"] = ref
        else:
            if not _ENV_NAME_PATTERN.match(ref):
                return RedirectResponse(
                    url="/admin/config?error=invalid+env+var+name",
                    status_code=303,
                )
            updates["openai_compatible_api_key"] = f"${{ENV:{ref}}}"
    try:
        with audit_required_effect(
            actor="admin",
            action="admin.overlay_write",
            payload={"keys": sorted(updates.keys()), "source": "html_form"},
        ):
            write_overlay(updates)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin/config?error={str(exc)[:160]}", status_code=303)
    return RedirectResponse(url="/admin/config?saved=1", status_code=303)


@router.get(
    "/admin/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def admin_test_page():
    return render_response(
        "admin/test.html",
        page_title="Test",
        active="test",
        admin_nav=ADMIN_NAV,
        providers=list_provider_choices(),
        config=current_config_view(),
        result=None,
        chosen_provider=None,
        chosen_model=None,
    )


@router.post(
    "/admin/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def admin_test_run(
    provider: str = Form(...),
    model: str = Form(...),
):
    result = test_provider_connection(provider, model)
    return render_response(
        "admin/test.html",
        page_title="Test",
        active="test",
        admin_nav=ADMIN_NAV,
        providers=list_provider_choices(),
        config=current_config_view(),
        result=result,
        chosen_provider=provider,
        chosen_model=model,
    )


@router.get(
    "/admin/secrets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_install_admin_html)],
)
def admin_secrets_page():
    names = known_env_names()
    return render_response(
        "admin/secrets.html",
        page_title="Secrets",
        active="secrets",
        admin_nav=ADMIN_NAV,
        names=names,
        status=env_var_status(names),
        catalog=env_catalog_with_status(),
    )


@router.get(
    "/admin/healthz",
    response_class=JSONResponse,
    dependencies=[Depends(require_install_admin)],
)
def admin_healthz() -> dict[str, Any]:
    """Lightweight self-check the admin UI calls to verify auth is wired."""
    return {"ok": True, "overlay_path": settings.runtime_overlay}


@router.post(
    "/admin/api/sessions/reap",
    dependencies=[Depends(require_install_admin)],
)
def api_sessions_reap() -> dict[str, Any]:
    """Sweep zombie agent sessions (process dead, status still 'active').

    Returns the list of session ids that were closed by this call so the
    operator can confirm what was reaped.
    """
    from brains.control.sessions import reap_zombie_sessions

    reaped = reap_zombie_sessions()
    return {"reaped": reaped, "count": len(reaped)}
