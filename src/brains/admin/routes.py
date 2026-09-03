"""Authentication endpoints retained for the modern ``/app`` console."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from brains.api.auth import (
    BROWSER_AUTH_COOKIE,
    BROWSER_COOKIE_TTL_SECONDS,
    _matches_any_key,
    _valid_keys,
    mint_browser_token,
)
from brains.web import render_response

router = APIRouter()


def _console_principal_for(key: str):
    """Resolve a raw key to a principal allowed to hold a console cookie."""
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
    target = next if (next and next.startswith("/") and not next.startswith("//")) else "/app"
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


__all__ = ["router"]
