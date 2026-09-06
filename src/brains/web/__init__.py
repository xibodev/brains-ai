"""Minimal rendering support for the modern console sign-in page."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(resources.files("brains.web")) / "templates"  # type: ignore[arg-type]
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render_response(
    template: str, /, *, request: Request | None = None, **context: Any
) -> HTMLResponse:
    if request is not None:
        context.setdefault("request", request)
    return HTMLResponse(_env.get_template(template).render(**context))


__all__ = ["TEMPLATES_DIR", "render_response"]
