"""Mount the built operator SPA (WS4) on the FastAPI app.

The SPA is the supported browser surface. It is built by
``frontend/`` (Vite) into ``brains/web/spa`` and shipped in the wheel via
package-data (see ``pyproject.toml``).

Serving uses ``importlib.resources`` path resolution so it works editable + wheel:

- Static assets are mounted at ``/app`` (StaticFiles, ``html=True``).
- A catch-all GET under ``/app/{path}`` serves ``index.html`` so the
  client-side router resolves deep links (SPA history fallback). The HTML
  entry is gated by :func:`require_browser_auth` (cookie auth) — the SPA
  signs in via ``/admin/login`` exactly like the admin console.

If the build directory is absent (dev checkout without an ``npm run
build``), the mount degrades gracefully: it logs a warning and skips,
rather than crashing app import.

The gateway wires this in ``main.py``::

    from brains.web.spa import mount_spa
    mount_spa(app)
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from brains.api.auth import require_browser_auth_html

logger = logging.getLogger(__name__)

# Resolve the on-disk location of the packaged SPA bundle. Using
# importlib.resources keeps us correct in editable, pipx, or wheel installs.
_PKG_ROOT = Path(resources.files("brains.web"))  # type: ignore[arg-type]
SPA_DIR: Path = _PKG_ROOT / "spa"
SPA_URL_PREFIX = "/app"


def mount_spa(app: FastAPI) -> None:
    """Mount the built SPA at ``/app`` with a history fallback.

    No-op (with a warning) when the bundle is missing, so a dev checkout
    that hasn't run ``npm run build`` still imports cleanly.
    """

    index_file = SPA_DIR / "index.html"
    if not index_file.is_file():
        logger.warning(
            "brains.web SPA bundle missing at %s; skipping /app mount. "
            "Run `cd frontend && npm install && npm run build` to populate it.",
            SPA_DIR,
        )
        return

    async def spa_index(
        request: Request,  # noqa: ARG001 - required for auth dependency context
        _auth: None = Depends(require_browser_auth_html),
    ) -> HTMLResponse:
        """Serve the SPA entry HTML (auth-gated; redirects to /admin/login when
        signed out so a browser hitting /app reaches the sign-in form)."""

        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    # Entry + history fallback. Registered BEFORE the StaticFiles mount so the
    # authed HTML entry wins for navigations; hashed assets fall through to the
    # StaticFiles mount below.
    app.add_api_route(
        SPA_URL_PREFIX,
        spa_index,
        methods=["GET"],
        include_in_schema=False,
        response_model=None,
    )

    async def spa_fallback(
        full_path: str,
        request: Request,  # noqa: ARG001
        _auth: None = Depends(require_browser_auth_html),
    ) -> FileResponse | HTMLResponse:
        """History fallback: real files are served, unknown paths get index."""

        candidate = (SPA_DIR / full_path).resolve()
        try:
            candidate.relative_to(SPA_DIR.resolve())
        except ValueError:
            # path traversal attempt — fall back to the app entry
            return HTMLResponse(index_file.read_text(encoding="utf-8"))
        if candidate.is_file():
            return FileResponse(candidate)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    app.add_api_route(
        f"{SPA_URL_PREFIX}/{{full_path:path}}",
        spa_fallback,
        methods=["GET"],
        include_in_schema=False,
        response_model=None,
    )

    # Static mount for the hashed asset bundle (CSS/JS/fonts). html=True lets
    # it resolve directory roots; the API routes above own the entry + deep
    # links so client-side routing works on hard refresh.
    app.mount(
        f"{SPA_URL_PREFIX}/assets",
        StaticFiles(directory=str(SPA_DIR / "assets")),
        name="brains-spa-assets",
    )

    logger.info("Mounted Brains operator SPA at %s (from %s)", SPA_URL_PREFIX, SPA_DIR)


# Brand mark served at the root ``/favicon.ico`` so browsers stop logging a 404
# on first document load (the request is made by the browser chrome, not the
# SPA, so it can't live under ``/app``). A tiny inline SVG keeps it asset-free.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#0e0e11'/>"
    "<path d='M16 6 L26 16 L16 26 L6 16 Z' fill='none' stroke='#cdd3ff' "
    "stroke-width='2.4' stroke-linejoin='round'/>"
    "</svg>"
)


def mount_favicon(app: FastAPI) -> None:
    """Serve a brand favicon at ``/favicon.ico`` (idempotent-safe to call once)."""

    async def favicon() -> Response:
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    app.add_api_route(
        "/favicon.ico",
        favicon,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )


__all__ = ["SPA_DIR", "SPA_URL_PREFIX", "mount_spa", "mount_favicon"]
