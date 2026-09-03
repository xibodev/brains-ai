"""Fail-closed compatibility API for the withdrawn feature installer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brains.extras import EXTRAS

SPECS: dict[str, object] = {}
VALID_FEATURES: tuple[str, ...] = ()


@dataclass
class WizardPlan:
    """An inert historical plan type retained for import compatibility."""

    features_to_enable: list[str] = field(default_factory=list)
    features_to_disable: list[str] = field(default_factory=list)
    extras_to_install: list[str] = field(default_factory=list)
    overlay_updates: dict[str, Any] = field(default_factory=dict)
    skipped_no_change: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return False

    @property
    def pip_command(self) -> None:
        return None


def _reject_requested(features: list[str] | None) -> None:
    if features:
        raise ValueError(
            f"Optional capabilities are withdrawn from Brains core: {sorted(features)}"
        )


def status_report() -> dict[str, Any]:
    return {"features": []}


def plan_changes(
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    features: list[str] | None = None,
) -> WizardPlan:
    _reject_requested(enable)
    _reject_requested(disable)
    _reject_requested(features)
    return WizardPlan()


def apply_plan(plan: WizardPlan, run_pip: bool = False) -> dict[str, Any]:
    if plan.features_to_enable or plan.extras_to_install or plan.overlay_updates or run_pip:
        raise ValueError("Optional capability installation is withdrawn from Brains core")
    return {
        "overlay_written": False,
        "pip_executed": False,
        "pip_command": None,
        "pip_returncode": None,
    }


def format_plan(plan: WizardPlan) -> str:
    return "No optional capabilities are shipped by Brains core."


__all__ = [
    "EXTRAS",
    "SPECS",
    "VALID_FEATURES",
    "WizardPlan",
    "apply_plan",
    "format_plan",
    "plan_changes",
    "status_report",
]
