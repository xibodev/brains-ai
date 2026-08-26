"""Static contract for the workspace-first operator SPA."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_coordination_and_email_live_in_canonical_app() -> None:
    app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    sidebar = (ROOT / "frontend/src/components/Sidebar.tsx").read_text(encoding="utf-8")
    config = (ROOT / "frontend/src/screens/Config.tsx").read_text(encoding="utf-8")
    coordination = (ROOT / "frontend/src/screens/OperatorCoordination.tsx").read_text(
        encoding="utf-8"
    )
    operations = (ROOT / "frontend/src/screens/Operations.tsx").read_text(encoding="utf-8")

    assert 'path="/coordination"' in app
    assert 'to: "/coordination"' in sidebar
    assert 'path="/operations/config/:section"' in app
    assert 'navigate("/operations/config")' in operations
    assert "AES-256-GCM" in config
    assert "Secret values are never returned" in config
    assert "Live agents" in coordination
    assert "Protected readiness" in operations
