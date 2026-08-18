"""Tests for the shared ``brains.web`` rendering layer.

Covers:
  * package-data wiring (templates + static found via importlib.resources)
  * Jinja2 environment exposes ``icon`` and ``static_url``
  * icon registry covers every icon name referenced by shipped templates
  * static mount is registered and serves ``brains.css``
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from brains.web import (
    STATIC_DIR,
    STATIC_URL_PREFIX,
    TEMPLATES_DIR,
    env,
    mount_static,
    render,
)
from brains.web import (
    icons as icons_module,
)


def test_template_directory_exists_in_installed_package():
    assert TEMPLATES_DIR.is_dir()
    assert (TEMPLATES_DIR / "admin" / "base.html").is_file()
    assert (TEMPLATES_DIR / "admin" / "overview.html").is_file()
    assert (TEMPLATES_DIR / "partials" / "_html.html").is_file()
    assert (TEMPLATES_DIR / "partials" / "_macros.html").is_file()


def test_static_directory_exists_and_contains_brand_css():
    assert STATIC_DIR.is_dir()
    css = (STATIC_DIR / "brains.css").read_text(encoding="utf-8")
    # Token primitives are the load-bearing claim of premium design.
    assert "--color-accent" in css
    assert "--space-" in css
    assert ".stat-card" in css
    assert ".panel" in css


def test_env_exposes_icon_and_static_url_globals():
    e = env()
    assert "icon" in e.globals
    assert "static_url" in e.globals
    url = e.globals["static_url"]("brains.css")
    assert url == f"{STATIC_URL_PREFIX}/brains.css"


def test_icon_returns_inline_svg_and_uses_class():
    svg = str(icons_module.icon("sliders"))
    assert svg.startswith("<svg")
    assert 'class="icon "' in svg or 'class="icon' in svg
    assert 'stroke="currentColor"' in svg


def test_icon_unknown_name_raises():
    with pytest.raises(KeyError):
        icons_module.icon("definitely-not-a-real-icon")


def test_every_template_icon_reference_is_defined():
    """Walk every template; every ``icon('name')`` must resolve."""

    available = set(icons_module.available_icons())
    pattern = re.compile(r"icon\(\s*['\"]([a-z][a-z0-9-]+)['\"]")
    missing: list[tuple[Path, str]] = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = match.group(1)
            if name not in available:
                missing.append((path, name))
    assert not missing, f"unresolved icons referenced in templates: {missing}"


def test_render_admin_overview_with_minimum_context_works():
    html = render(
        "admin/overview.html",
        page_title="Overview",
        active="overview",
        admin_nav=[
            ("overview", "Overview", "/admin/overview", "circle-dot"),
            ("config", "Config", "/admin/config", "sliders"),
            ("test", "Test connection", "/admin/test", "plug-zap"),
            ("secrets", "Secrets", "/admin/secrets", "lock"),
        ],
        overview={
            "greeting": "Control plane",
            "tagline": "Routing AI agents across model tiers.",
            "providers": ["echo", "ollama"],
            "tier_count": 0,
            "route_count": 0,
            "models": {},
            "routes": {},
            "rate_limit_per_minute": 0,
            "api_key_set": True,
            "api_keys_count": 1,
            "openai_compatible_api_key_set": False,
            "allow_unauthenticated_api": False,
            "overlay_path": "/tmp/overlay.yaml",
        },
    )
    # Brand mark + stylesheet + page title surface in the shell.
    assert "Brains Admin · Overview" in html
    assert "brand-mark" in html
    assert "brains.css" in html
    # Premium primitives present.
    assert "stat-card" in html
    assert "Control plane" in html
    # Empty-state CTA replaces the old "(none)" td.
    assert "No tiers defined" in html
    assert "No routes mapped" in html


def test_mount_static_serves_css_via_test_client():
    app = FastAPI()
    mount_static(app)
    client = TestClient(app)
    response = client.get(f"{STATIC_URL_PREFIX}/brains.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert "--color-accent" in response.text
