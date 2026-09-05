"""Compatibility boundary for withdrawn optional product extras.

Historical configuration and migrations may name these extras, but a shipped
Brains process must never advertise or activate them.
"""

from __future__ import annotations

from dataclasses import dataclass


class ExtraNotInstalledError(RuntimeError):
    """Raised when code attempts to activate a withdrawn extra."""


@dataclass(frozen=True)
class Extra:
    """Historical extra record retained for import compatibility."""

    name: str
    probe_modules: tuple[str, ...]
    description: str


EXTRAS: dict[str, Extra] = {}


def is_extra_installed(name: str) -> bool:
    """Withdrawn extras are never available to the runtime."""
    return False


def require_extra(name: str, subsystem: str) -> None:
    """Fail closed for every withdrawn extra, regardless of installed drivers."""
    raise ExtraNotInstalledError(
        f"Optional capability {name!r} for {subsystem!r} is withdrawn from Brains core."
    )


def installed_extras() -> dict[str, bool]:
    """Return the shipped optional-capability inventory (empty by design)."""
    return {}
