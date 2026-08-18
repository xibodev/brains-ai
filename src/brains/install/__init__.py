"""`brains-ai features` interactive + scriptable subsystem wizard.

The wizard is the operator-facing payoff of the Step 0 plugin/extras
refactor: it makes optional subsystems discoverable, prints the exact
`pip install` commands, and (with explicit confirmation) edits
``brains.runtime.yaml`` for the operator.

Design rules — DO NOT relax without explicit user approval:

1. The wizard NEVER runs `pip install` silently. Operator must either
   answer "yes" at the interactive prompt or pass ``--run-pip`` on the
   command line. By default we print the command and exit so the operator
   can copy-paste it.
2. The wizard NEVER guesses extras for subsystems the operator did not
   enable. ``--enable telegram`` only sets that one flag.
3. The wizard ONLY writes config keys that are already in
   :data:`brains.config.ADMIN_EDITABLE_KEYS` — the ``write_overlay``
   helper enforces that.
4. The wizard is safe in tests: every side-effecting step
   (overlay write, pip execution) is gated by a flag so tests can drive
   it with ``dry_run=True``.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from brains.admin.service import read_overlay, write_overlay
from brains.config import RUNTIME_OVERLAY_SCHEMA_VERSION, settings
from brains.extras import EXTRAS, installed_extras


# Subsystem name → (extra name, overlay-path setter)
# Each setter takes the current overlay dict and either enables (True) or
# disables (False) the subsystem in-place. The wizard composes these into
# a single overlay diff before calling ``write_overlay``.
def _set_bridge(name: str) -> _SubsystemSpec:
    def setter(overlay: dict[str, Any], enabled: bool) -> None:
        subs = overlay.setdefault("subsystems", {})
        bridges = subs.setdefault("bridges", {})
        bridges.setdefault(name, {})["enabled"] = enabled

    def reader(overlay: dict[str, Any]) -> bool:
        return bool(
            overlay.get("subsystems", {}).get("bridges", {}).get(name, {}).get("enabled", False)
        )

    return _SubsystemSpec(
        feature=name,
        extra=name,
        label=f"{name.title()} bridge",
        description=EXTRAS[name].description,
        setter=setter,
        reader=reader,
    )


def _postgres_spec() -> _SubsystemSpec:
    def setter(overlay: dict[str, Any], enabled: bool) -> None:
        subs = overlay.setdefault("subsystems", {})
        storage = subs.setdefault("storage", {})
        storage["backend"] = "postgres" if enabled else "sqlite"

    def reader(overlay: dict[str, Any]) -> bool:
        return (
            overlay.get("subsystems", {}).get("storage", {}).get("backend", "sqlite") == "postgres"
        )

    return _SubsystemSpec(
        feature="postgres",
        extra="postgres",
        label="Postgres backend (replaces SQLite)",
        description=EXTRAS["postgres"].description,
        setter=setter,
        reader=reader,
    )


def _otel_spec() -> _SubsystemSpec:
    def setter(overlay: dict[str, Any], enabled: bool) -> None:
        subs = overlay.setdefault("subsystems", {})
        subs.setdefault("otel", {})["enabled"] = enabled

    def reader(overlay: dict[str, Any]) -> bool:
        return bool(overlay.get("subsystems", {}).get("otel", {}).get("enabled", False))

    return _SubsystemSpec(
        feature="otel",
        extra="otel",
        label="OpenTelemetry exports",
        description=EXTRAS["otel"].description,
        setter=setter,
        reader=reader,
    )


def _litellm_spec() -> _SubsystemSpec:
    """LiteLLM has no subsystem flag — installing the extra is the only step.

    The setter/reader are no-ops because there's no ``subsystems.litellm``
    key. The wizard still exposes it so operators can pick "yes please
    install LiteLLM" from the same prompt.
    """

    def setter(overlay: dict[str, Any], enabled: bool) -> None:
        return None

    def reader(overlay: dict[str, Any]) -> bool:
        # "Enabled" means the extra is installed. There's no config flag.
        return installed_extras().get("litellm", False)

    return _SubsystemSpec(
        feature="litellm",
        extra="litellm",
        label="LiteLLM provider",
        description=EXTRAS["litellm"].description,
        setter=setter,
        reader=reader,
        config_flag=False,
    )


@dataclass(frozen=True)
class _SubsystemSpec:
    feature: str
    extra: str
    label: str
    description: str
    setter: Any
    reader: Any
    # If False, the wizard does not touch the overlay for this feature
    # (extra-only). Defaults to True (most features set a flag).
    config_flag: bool = True


def _spec_catalog() -> dict[str, _SubsystemSpec]:
    return {
        spec.feature: spec
        for spec in (
            _set_bridge("telegram"),
            _set_bridge("slack"),
            _set_bridge("whatsapp"),
            _postgres_spec(),
            _otel_spec(),
            _litellm_spec(),
        )
    }


SPECS = _spec_catalog()
VALID_FEATURES = tuple(SPECS.keys())


@dataclass
class WizardPlan:
    """The full set of changes the wizard would apply.

    Always built before any side effect runs so the operator can see and
    confirm everything in one place.
    """

    features_to_enable: list[str] = field(default_factory=list)
    features_to_disable: list[str] = field(default_factory=list)
    extras_to_install: list[str] = field(default_factory=list)
    overlay_updates: dict[str, Any] = field(default_factory=dict)
    skipped_no_change: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.features_to_enable
            or self.features_to_disable
            or self.extras_to_install
            or self.overlay_updates
        )

    @property
    def pip_command(self) -> list[str] | None:
        if not self.extras_to_install:
            return None
        extras_spec = ",".join(sorted(self.extras_to_install))
        return [sys.executable, "-m", "pip", "install", f"brains-ai[{extras_spec}]"]


def _normalize_features(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [piece.strip() for piece in value.split(",") if piece.strip()]
    else:
        items = [str(piece).strip() for piece in value if str(piece).strip()]
    unknown = sorted(set(items) - set(VALID_FEATURES))
    if unknown:
        raise ValueError(f"Unknown features: {unknown}. Valid: {list(VALID_FEATURES)}")
    # Preserve order but drop dupes
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def status_report() -> dict[str, Any]:
    """Return a structured snapshot of every feature's current state."""
    overlay = read_overlay()
    installed = installed_extras()
    rows: list[dict[str, Any]] = []
    for spec in SPECS.values():
        rows.append(
            {
                "feature": spec.feature,
                "label": spec.label,
                "description": spec.description,
                "extra_installed": installed.get(spec.extra, False),
                "config_enabled": spec.reader(overlay),
            }
        )
    return {
        "schema_version": RUNTIME_OVERLAY_SCHEMA_VERSION,
        "overlay_path": settings.runtime_overlay,
        "features": rows,
    }


def plan_changes(
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    features: list[str] | None = None,
) -> WizardPlan:
    """Compute the plan without applying it.

    Exactly one of ``features`` (full set, replaces current selection) or
    the ``enable``/``disable`` deltas should be provided. If ``features`` is
    given, ``enable``/``disable`` are ignored.
    """
    overlay = read_overlay()
    installed = installed_extras()
    plan = WizardPlan()
    updated_overlay = _deep_copy(overlay)

    if features is not None:
        wanted = set(features)
        # Enable the wanted set, disable anything else that's currently on.
        for spec in SPECS.values():
            if not spec.config_flag:
                # Extra-only feature: track install need, no overlay write.
                if spec.feature in wanted and not installed.get(spec.extra, False):
                    plan.extras_to_install.append(spec.extra)
                continue
            currently_on = spec.reader(overlay)
            should_be_on = spec.feature in wanted
            if should_be_on and not currently_on:
                plan.features_to_enable.append(spec.feature)
                spec.setter(updated_overlay, True)
                if not installed.get(spec.extra, False):
                    plan.extras_to_install.append(spec.extra)
            elif not should_be_on and currently_on:
                plan.features_to_disable.append(spec.feature)
                spec.setter(updated_overlay, False)
            else:
                plan.skipped_no_change.append(spec.feature)
    else:
        for feature in enable or []:
            spec = SPECS[feature]
            if not spec.config_flag:
                if not installed.get(spec.extra, False):
                    plan.extras_to_install.append(spec.extra)
                continue
            if spec.reader(overlay):
                plan.skipped_no_change.append(feature)
                continue
            plan.features_to_enable.append(feature)
            spec.setter(updated_overlay, True)
            if not installed.get(spec.extra, False):
                plan.extras_to_install.append(spec.extra)
        for feature in disable or []:
            spec = SPECS[feature]
            if not spec.config_flag:
                plan.skipped_no_change.append(feature)
                continue
            if not spec.reader(overlay):
                plan.skipped_no_change.append(feature)
                continue
            plan.features_to_disable.append(feature)
            spec.setter(updated_overlay, False)

    if updated_overlay != overlay:
        # Only include the ``subsystems`` block in the diff — write_overlay
        # merges, so we don't need to resend everything.
        diff = {}
        new_subs = updated_overlay.get("subsystems")
        if new_subs is not None:
            diff["subsystems"] = new_subs
        plan.overlay_updates = diff
    return plan


def apply_plan(plan: WizardPlan, run_pip: bool = False) -> dict[str, Any]:
    """Apply a planned change set. Returns a structured result.

    ``run_pip`` controls whether we actually shell out to ``pip install``.
    Defaults to False — the safer behaviour is to print the command and
    leave it for the operator. Callers must opt in.
    """
    result: dict[str, Any] = {
        "overlay_written": False,
        "pip_executed": False,
        "pip_command": None,
        "pip_returncode": None,
    }
    if plan.overlay_updates:
        # reload=False is critical: enabling a subsystem before its extra
        # is installed would otherwise trip the startup gate immediately
        # and abort the write we were authorised to make. The gate still
        # fires correctly on the next process start.
        write_overlay(plan.overlay_updates, reload=False)
        result["overlay_written"] = True
    pip_cmd = plan.pip_command
    if pip_cmd is not None:
        result["pip_command"] = pip_cmd
        if run_pip:
            completed = subprocess.run(pip_cmd, check=False)
            result["pip_executed"] = True
            result["pip_returncode"] = completed.returncode
    return result


def format_plan(plan: WizardPlan) -> str:
    """Human-friendly plan summary for the CLI."""
    if not plan.has_changes:
        return "Nothing to do — config already matches request."
    lines: list[str] = []
    if plan.features_to_enable:
        lines.append("Will enable:  " + ", ".join(plan.features_to_enable))
    if plan.features_to_disable:
        lines.append("Will disable: " + ", ".join(plan.features_to_disable))
    if plan.extras_to_install:
        lines.append("Will install: brains-ai[" + ",".join(plan.extras_to_install) + "]")
        pip_cmd = plan.pip_command
        if pip_cmd is not None:
            lines.append("  $ " + " ".join(shlex.quote(arg) for arg in pip_cmd))
    if plan.overlay_updates:
        lines.append(f"Overlay edits: {plan.overlay_updates}")
    return "\n".join(lines)


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


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
