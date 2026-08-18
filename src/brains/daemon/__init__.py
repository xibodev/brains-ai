"""The brains daemon — one long-lived process per machine.

`detect CLIs → register runtimes → heartbeat → poll/claim → spawn via
exec.runner → stream events → GC`. It reuses claims, the tool registry, machine
identity, and the current operator-key auth model. The execution and credential
boundaries are documented in ``docs/ARCHITECTURE.md``.
"""

from brains.daemon.config import DaemonConfig, load_config
from brains.daemon.daemon import Daemon

__all__ = ["Daemon", "DaemonConfig", "load_config"]
