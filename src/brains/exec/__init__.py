"""Brains executor — gated multi-turn agent CLI sessions.

The executor runs an agent CLI (Copilot / Claude / Codex) inside a sandbox
worktree with brains *action-shims* on PATH so every outward action (push,
deploy, DNS, money, remote/prod network) is intercepted and routed through the
brains approval queue (read+propose posture). See :mod:`brains.exec.gate`.
"""

from brains.exec.runner import GATED_BINARIES, install_shims, run_session

__all__ = ["GATED_BINARIES", "install_shims", "run_session"]
