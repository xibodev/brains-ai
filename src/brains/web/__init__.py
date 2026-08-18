"""Web rendering layer for brains.

Centralises the Jinja2 environment and static-asset mount so the admin
console + dashboard share one design system. See ``static/brains.css``
for the token + component layer and ``templates/`` for the layouts.

The package ships its own templates + static via package-data declared
in ``pyproject.toml``.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from brains.web import filters as _filters
from brains.web import icons as _icons

# Resolve the on-disk locations of the packaged template + static
# directories. Using importlib.resources keeps us correct whether
# brains is installed in editable mode, via pipx, or from a wheel.
_PKG_ROOT = Path(resources.files("brains.web"))  # type: ignore[arg-type]
TEMPLATES_DIR: Path = _PKG_ROOT / "templates"
STATIC_DIR: Path = _PKG_ROOT / "static"
STATIC_URL_PREFIX = "/static/brains"


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        enable_async=False,
    )
    env.globals["icon"] = _icons.icon
    env.globals["static_url"] = lambda path: f"{STATIC_URL_PREFIX}/{path.lstrip('/')}"
    env.filters["relative_time"] = _filters.relative_time
    env.filters["is_test_pollution"] = _filters.is_test_pollution
    env.tests["test_pollution"] = _filters.is_test_pollution
    return env


_env: Environment = _build_env()


def env() -> Environment:
    """Return the (cached) Jinja2 environment."""

    return _env


def render(template: str, /, **context: Any) -> str:
    """Render ``template`` with the shared environment."""

    tmpl = _env.get_template(template)
    return tmpl.render(**context)


def render_response(
    template: str, /, *, request: Request | None = None, **context: Any
) -> HTMLResponse:
    """Render a template and wrap it in an ``HTMLResponse``.

    ``request`` is accepted to mirror the ``Jinja2Templates`` API and
    will be added to the context when present, so templates can pull
    request-scoped values (path, query string) without bespoke wiring.
    """

    if request is not None:
        context.setdefault("request", request)
    return HTMLResponse(render(template, **context))


def mount_static(app: FastAPI) -> None:
    """Mount the packaged static directory on a FastAPI app.

    The mount point is intentionally namespaced (``/static/brains``)
    so consumers can layer their own ``/static`` mount later without
    collision.
    """

    if not STATIC_DIR.is_dir():  # pragma: no cover - install integrity
        raise RuntimeError(
            f"brains.web static directory missing at {STATIC_DIR!s}; "
            "package data likely not installed (check pyproject.toml)"
        )
    app.mount(
        STATIC_URL_PREFIX,
        StaticFiles(directory=str(STATIC_DIR)),
        name="brains-static",
    )


__all__ = [
    "STATIC_DIR",
    "STATIC_URL_PREFIX",
    "TEMPLATES_DIR",
    "env",
    "mount_static",
    "render",
    "render_response",
]
