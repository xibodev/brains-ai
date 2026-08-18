"""Capability-aware orientation policy.

Smaller or navigation-prone models receive proactive repository orientation.
Strong navigators skip it by default to avoid unnecessary context overhead.
Unknown models receive orientation because the expected downside is additional
context, while omission can prevent a weaker model from locating relevant code.

This is a model-name heuristic, not a universal quality or cost guarantee.
"""

from __future__ import annotations

# Substrings that mark a STRONG navigator → skip orientation by default.
_STRONG_MARKERS = (
    "opus",
    "sonnet",
    "gpt-5-codex",
    "codex",
    "gpt-5.",  # gpt-5.x full (not -mini)
    "o1",
    "o3",
    "gemini-2.5-pro",
    "gemini-1.5-pro",
)

# Substrings that mark a WEAK/CHEAP model → always inject.
_WEAK_MARKERS = (
    "haiku",
    "mini",
    "0.5b",
    "1.5b",
    "1b",
    "2b",
    "3b",
    "7b",
    "8b",
    "qwen2.5-coder",
    "llama3.2",
    "phi",
    "gemma",
    "small",
    "flash",
)


def should_orient(model: str | None) -> bool:
    """Return True if orientation should be auto-injected for ``model``.

    Explicit caller intent (passing an ``orient_query`` or not) always wins at the
    call site; this is only consulted for the *auto* decision.
    """
    if not model:
        # No model pinned = the CLI's default (usually a strong frontier model in
        # Copilot/Claude/Codex) → don't inject.
        return False
    m = model.lower()
    # Strong markers are the more specific signal → checked first so a generic
    # weak substring (e.g. "mini" inside "geMINI") can't misclassify a strong model.
    if any(s in m for s in _STRONG_MARKERS):
        return False
    if any(_weak_hit(w, m) for w in _WEAK_MARKERS):
        return True
    # Unknown → inject (safe asymmetry: cost vs correctness).
    return True


def _weak_hit(marker: str, model: str) -> bool:
    """Match a weak marker. Size markers like ``mini``/``3b`` must be bounded by a
    non-alphanumeric (or string edge) so they don't match inside another word
    (``mini`` in ``gemini``, ``3b`` in ``x3basd``)."""
    idx = model.find(marker)
    while idx != -1:
        before = model[idx - 1] if idx > 0 else ""
        after = model[idx + len(marker)] if idx + len(marker) < len(model) else ""
        if not before.isalnum() and not after.isalnum():
            return True
        idx = model.find(marker, idx + 1)
    return False


__all__ = ["should_orient"]
