"""Optional-extras registry and the subsystem-gating helper.

Brains keeps the lean-core install small (`pip install brains`). Heavier or
vendor-specific features ship as **pip extras** declared in
``pyproject.toml`` (``brains-ai[postgres]``, ``brains-ai[telegram]``, etc.) and as
**config-gated runtime subsystems** in ``brains.runtime.yaml``.

Two strict rules:

1. Importing this module **never** fails because of a missing extra. We only
   check at the boundary where a subsystem is enabled.
2. If a subsystem is enabled in config but its extra is not installed, we
   **fail loud at startup** with the exact ``pip install`` command. We never
   silent-skip and we never shell out to pip at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


class ExtraNotInstalledError(RuntimeError):
    """Raised when a subsystem is enabled in config without its pip extra.

    The message always contains the exact remediation command so the operator
    can fix it in one shot without grepping docs.
    """


@dataclass(frozen=True)
class Extra:
    """A declared optional feature: ``name`` -> set of import-probe modules."""

    name: str
    probe_modules: tuple[str, ...]
    description: str


# Single source of truth for every optional extra. Mirrors
# ``[project.optional-dependencies]`` in ``pyproject.toml``. When you add a
# new extra there, add it here too — the test suite enforces this.
EXTRAS: dict[str, Extra] = {
    "litellm": Extra(
        name="litellm",
        probe_modules=("litellm",),
        description="LiteLLM provider (unified API over OpenAI / Anthropic / Bedrock / Vertex / Mistral).",
    ),
    "postgres": Extra(
        name="postgres",
        probe_modules=("asyncpg", "psycopg"),
        description="Postgres storage backend (alternative to the default SQLite).",
    ),
    "telegram": Extra(
        name="telegram",
        probe_modules=("telegram",),  # python-telegram-bot imports as `telegram`
        description="Telegram bridge: approve gates and message sessions from Telegram.",
    ),
    "slack": Extra(
        name="slack",
        probe_modules=("slack_sdk",),
        description="Slack bridge: push agent done/stuck notifications and accept approvals.",
    ),
    "whatsapp": Extra(
        name="whatsapp",
        # WhatsApp bridge uses the Meta Cloud API over plain HTTPS; httpx is
        # already in the lean core so no module probe is strictly needed.
        # We keep the extra as a marker for the wizard and future deps.
        probe_modules=(),
        description="WhatsApp bridge (Meta Cloud API): approve gates from WhatsApp.",
    ),
    "whatsapp_web": Extra(
        name="whatsapp_web",
        # WhatsApp Web bridge POSTs to the local wa-web sidecar over httpx
        # (already in the lean core), so no module probe is needed. Marker extra.
        probe_modules=(),
        description="WhatsApp Web bridge (companion-device via the wa-web sidecar): approve gates from a self-hosted WhatsApp link.",
    ),
    "otel": Extra(
        name="otel",
        probe_modules=("opentelemetry.sdk", "opentelemetry.exporter.otlp"),
        description="OpenTelemetry traces + structured logs export.",
    ),
}


def is_extra_installed(name: str) -> bool:
    """Return True iff every probe module for ``name`` imports cleanly.

    Unknown extra names return False so a typo'd config doesn't accidentally
    pass the gate.
    """
    extra = EXTRAS.get(name)
    if extra is None:
        return False
    for module_name in extra.probe_modules:
        try:
            import_module(module_name)
        except ImportError:
            return False
    return True


def require_extra(name: str, subsystem: str) -> None:
    """Raise :class:`ExtraNotInstalledError` if ``name`` is missing.

    ``subsystem`` is a human-readable label (e.g. ``"bridges.telegram"``)
    that's included in the error message so the operator knows which config
    block triggered the check.
    """
    extra = EXTRAS.get(name)
    if extra is None:
        raise ExtraNotInstalledError(
            f"Unknown brains extra: {name!r} (referenced by {subsystem!r}). "
            f"Known extras: {sorted(EXTRAS)}."
        )
    if is_extra_installed(name):
        return
    raise ExtraNotInstalledError(
        f"{subsystem} requires the {name!r} extra, which is not installed.\n"
        f"  Description: {extra.description}\n"
        f"  Fix:         pip install 'brains-ai[{name}]'"
    )


def installed_extras() -> dict[str, bool]:
    """Snapshot of every declared extra and whether it's importable now.

    Used by ``brains health`` and the dashboard installer surface so the
    operator can see at a glance what's available without reading pip output.
    """
    return {name: is_extra_installed(name) for name in EXTRAS}
