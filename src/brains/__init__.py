"""Brains — local-first control plane and coordination layer for AI coding agents."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: read from installed package metadata so
    # `brains.__version__` cannot drift away from `pyproject.toml`'s
    # `version` field (which is what PyPI + pipx ship).
    __version__ = _pkg_version("brains-ai")
except PackageNotFoundError:  # pragma: no cover - editable/dev install w/o metadata
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
