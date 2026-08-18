"""Operator-control bridges (Telegram, Slack, WhatsApp, ...).

Each bridge is an **optional runtime subsystem** gated by a config block in
``brains.runtime.yaml`` *and* a pip extra in ``pyproject.toml``. The wiring
contract is intentionally boring:

1. Each bridge module exposes ``NAME``, ``status(settings)``,
   ``build_sender(...)``, and ``send(text, kind, correlation_id, sender)``.
2. We use **static imports keyed off the config** (not entry-points), so
   this file is the only place that knows which bridge modules exist.
   Tradeoff: easier to debug, no plugin-discovery edge cases; cost:
   adding a new bridge is a two-line change here.
3. The config gate in :func:`brains.config._enforce_subsystem_extras`
   fails loud if the extra is missing before any bridge module is
   imported, so the dispatch helpers below can safely defer-import the
   bridge modules when they're actually enabled.
4. Secrets (bot tokens, channel IDs, recipient numbers) come from env
   vars — never the runtime overlay. This matches the existing
   ``ENV_REF_ALLOWED_FIELDS`` policy.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from brains.bridges.base import (
    BridgeKind,
    BridgeMessage,
    BridgeStatus,
    Sender,
    send_message,
)

# Single source of truth: bridge name → module path. Adding a bridge means
# adding one row here plus the matching module file.
BRIDGE_MODULES: dict[str, str] = {
    "telegram": "brains.bridges.telegram",
    "slack": "brains.bridges.slack",
    "whatsapp": "brains.bridges.whatsapp",
    "whatsapp_web": "brains.bridges.whatsapp_web",
}


def enabled_bridges(settings_obj: Any) -> list[str]:
    """Return the names of the bridges currently enabled in config.

    Cheap helper for ``brains health`` and the dashboard — does not import
    any bridge module, so it's safe to call without the matching extras.
    """
    out: list[str] = []
    bridges = getattr(getattr(settings_obj, "subsystems", None), "bridges", None)
    if bridges is None:
        return out
    if getattr(bridges.telegram, "enabled", False):
        out.append("telegram")
    if getattr(bridges.slack, "enabled", False):
        out.append("slack")
    if getattr(bridges.whatsapp, "enabled", False):
        out.append("whatsapp")
    if getattr(getattr(bridges, "whatsapp_web", None), "enabled", False):
        out.append("whatsapp_web")
    return out


def status_all(settings_obj: Any) -> list[BridgeStatus]:
    """Return :class:`BridgeStatus` for every known bridge.

    Imports each bridge module lazily so missing extras don't break the
    snapshot — but the bridge modules themselves never import their SDK
    at module-load time (they defer until ``build_sender`` is called), so
    this is safe even with no extras installed at all.
    """
    out: list[BridgeStatus] = []
    for name, module_path in BRIDGE_MODULES.items():
        try:
            module = import_module(module_path)
            out.append(module.status(settings_obj))
        except ImportError as exc:
            out.append(
                BridgeStatus(
                    name=name,
                    enabled=False,
                    configured=False,
                    detail=f"import failed: {exc.name or 'unknown'}",
                )
            )
    return out


def get_bridge(name: str):
    """Import and return a bridge module by name.

    Raises :class:`KeyError` for unknown names; ``ImportError`` if the
    matching extra is missing.
    """
    return import_module(BRIDGE_MODULES[name])


__all__ = [
    "BRIDGE_MODULES",
    "BridgeKind",
    "BridgeMessage",
    "BridgeStatus",
    "Sender",
    "enabled_bridges",
    "get_bridge",
    "send_message",
    "status_all",
]
