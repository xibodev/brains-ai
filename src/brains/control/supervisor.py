"""Single-process supervisor for the brains stack.

Ported from agent-hivemind's ``hive-service`` (Phase 2 PR-4 of the
consolidation plan). Supervises by default:

* ``brains.main:app``        \u2014 the gateway FastAPI app on :8787

The MCP SSE server (``brains.mcp.server`` on :9877) is what agent tools
connect to, so it is supervised by default alongside the gateway. Pass
``--no-mcp`` to leave it out. Its bind host is driven by ``BRAINS_MCP_BIND``
/ ``BRAINS_MCP_ALLOW_PUBLIC`` per the MCP auth design.

The legacy dashboard (``brains.dashboard.app`` on :9876) is a retired
surface: it is supervised only when explicitly requested with
``--dashboard`` or ``BRAINS_LEGACY_SURFACES=1`` (see
``brains.experimental``). ``--no-dashboard`` remains accepted as a no-op
veto for back-compatibility.

Features:
* Combined logging to ``<state_dir>/sessions/service.log`` (rotated at 5MB).
* Restart-on-crash with exponential backoff (1s \u2192 60s, capped).
* SIGTERM / SIGINT propagation for graceful shutdown.
* PID file at ``<state_dir>/sessions/service.pid`` for OS service managers -
  an additive JSON record (pid, executable, command line, start time where
  portable) rather than a bare integer, so ``brains.service.common.verify_pid``
  can tell a live match from a reused PID (BL-P1-09).

Run:

    brains serve-all [--no-gateway] [--dashboard] [--no-mcp]
    python -m brains.control.supervisor [...]
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _state_dir() -> Path:
    """Resolve the directory under which logs and the PID file live.

    ``BRAINS_STATE_DIR`` overrides; otherwise we use ``~/.brains/``.
    """
    override = os.environ.get("BRAINS_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".brains"


def _log_path() -> Path:
    return _state_dir() / "sessions" / "service.log"


def _pid_path() -> Path:
    return _state_dir() / "sessions" / "service.pid"


logger = logging.getLogger("brains-supervisor")
logger.setLevel(logging.INFO)


def _setup_logging() -> None:
    sessions_dir = _state_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    if logger.handlers:
        return
    file_handler = logging.handlers.RotatingFileHandler(
        _log_path(), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


class Child:
    """Supervised child process with restart-on-crash + exponential backoff."""

    BACKOFF_INITIAL = 1.0
    BACKOFF_MAX = 60.0
    BACKOFF_RESET_AFTER = 60.0  # seconds of successful uptime reset the backoff

    def __init__(self, name: str, argv: list[str]) -> None:
        self.name = name
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.backoff = self.BACKOFF_INITIAL
        self.started_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise,
            name=f"sup-{self.name}",
            daemon=False,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            logger.info("stopping %s (pid=%s)", self.name, self.proc.pid)
            try:
                if os.name == "nt":
                    self.proc.terminate()
                else:
                    self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning("%s did not exit in %.1fs, killing", self.name, timeout)
                    self.proc.kill()
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.debug("stop %s: %r", self.name, exc)
        if self._thread:
            self._thread.join(timeout=timeout + 5)

    # -- supervisor loop --------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._spawn_once()
            except Exception as exc:  # pragma: no cover - logged + retried
                logger.exception("%s spawn failed: %r", self.name, exc)
            if self._stop.is_set():
                return
            sleep = min(self.backoff, self.BACKOFF_MAX)
            logger.warning("%s exited; restart in %.1fs", self.name, sleep)
            self._sleep(sleep)
            self.backoff = min(self.backoff * 2.0, self.BACKOFF_MAX)

    def _spawn_once(self) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        logger.info("starting %s: %s", self.name, " ".join(self.argv))
        self.started_at = time.monotonic()
        self.proc = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=1,
            text=True,
            errors="replace",
        )
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            logger.info("[%s] %s", self.name, line.rstrip())
            if self._stop.is_set():
                break
        rc = self.proc.wait()
        uptime = time.monotonic() - self.started_at
        logger.info("%s exited rc=%s after %.1fs", self.name, rc, uptime)
        if uptime >= self.BACKOFF_RESET_AFTER:
            self.backoff = self.BACKOFF_INITIAL

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, 0.5))


def _build_children(args: argparse.Namespace) -> list[Child]:
    children: list[Child] = []
    if not args.no_gateway:
        children.append(
            Child(
                "gateway",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "brains.main:app",
                    "--host",
                    args.gateway_host,
                    "--port",
                    str(args.gateway_port),
                ],
            )
        )
    if _include_legacy_dashboard(args):
        children.append(
            Child(
                "dashboard",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "brains.dashboard.app:app",
                    "--host",
                    args.dashboard_host,
                    "--port",
                    str(args.dashboard_port),
                ],
            )
        )
    if not args.no_mcp:
        children.append(
            Child(
                "mcp",
                [
                    sys.executable,
                    "-m",
                    "brains.mcp.server",
                    "--mode",
                    "sse",
                    "--port",
                    str(args.mcp_port),
                    "--scheduler-interval",
                    str(args.mcp_scheduler_interval),
                ],
            )
        )
    return children


#: Exit code for a configuration/preflight failure the supervisor cannot fix by
#: running. Service definitions that can filter on it (systemd's
#: ``RestartPreventExitStatus``) must not restart the unit for this code.
CONFIG_EXIT_CODE = 3

#: Default bounded window the supervisor stays up, retrying a blocked listener
#: bind, before it gives up with :data:`CONFIG_EXIT_CODE`. Service managers that
#: cannot filter on an exit code (launchd, Task Scheduler) see a slow, bounded
#: degraded hold instead of a tight relaunch loop.
DEFAULT_PREFLIGHT_WAIT_SECONDS = 300


def _preflight_wait_seconds() -> int:
    try:
        raw = int(os.environ.get("BRAINS_SUPERVISOR_PREFLIGHT_WAIT_SECONDS", ""))
    except ValueError:
        raw = DEFAULT_PREFLIGHT_WAIT_SECONDS
    return max(0, min(raw, 3600))


def _first_unbindable(
    listeners: list[tuple[str, str, int]],
) -> tuple[str, str, int] | None:
    """The first enabled listener whose actual bind is unavailable, retried.

    The supervisor holds a bounded degraded state rather than exiting on the
    first blocked bind: a conflict is usually a slower predecessor still
    shutting down. Returns ``None`` once every listener can bind, or the
    listener still blocked when the window closes.
    """
    deadline = time.monotonic() + _preflight_wait_seconds()
    delay = 1.0
    while True:
        blocked = next(
            (
                (name, host, port)
                for name, host, port in listeners
                if not _port_bindable(host, port)
            ),
            None,
        )
        if blocked is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return blocked
        logger.warning(
            "%s port %s:%s is unavailable; degraded, retrying for up to %.0fs",
            *blocked,
            remaining,
        )
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 60.0)


def _port_bindable(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _write_pidfile() -> None:
    from brains.service.common import write_pidfile

    write_pidfile(_pid_path())


def _clear_pidfile() -> None:
    with contextlib.suppress(OSError):
        _pid_path().unlink()


def _include_legacy_dashboard(args: argparse.Namespace) -> bool:
    """The legacy dashboard is retired from the normal install.

    It runs only when explicitly requested: ``--dashboard``, or the
    ``BRAINS_LEGACY_SURFACES`` opt-in. ``--no-dashboard`` (kept for
    back-compatibility) is a veto that wins over both.
    """
    if args.no_dashboard:
        return False
    from brains.experimental import legacy_surfaces_enabled

    return bool(getattr(args, "dashboard", False)) or legacy_surfaces_enabled()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brains-supervisor")
    parser.add_argument("--no-gateway", action="store_true")
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Back-compat veto: never start the legacy dashboard child.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Opt in to the retired legacy dashboard child (normally off).",
    )
    parser.add_argument("--no-mcp", action="store_true")
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=8787)
    parser.add_argument("--dashboard-host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=9876)
    parser.add_argument("--mcp-port", type=int, default=9877)
    parser.add_argument("--mcp-scheduler-interval", type=int, default=60)
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _setup_logging()
    logger.info(
        "brains-supervisor starting; state_dir=%s pid=%s",
        _state_dir(),
        os.getpid(),
    )

    listeners: list[tuple[str, str, int]] = []
    if not args.no_gateway:
        listeners.append(("gateway", args.gateway_host, args.gateway_port))
    if _include_legacy_dashboard(args):
        listeners.append(("dashboard", args.dashboard_host, args.dashboard_port))
    if not args.no_mcp:
        from brains.mcp.sse_auth import resolve_bind_host

        listeners.append(("mcp", resolve_bind_host(), args.mcp_port))
    ports: dict[int, str] = {}
    for name, _host, port in listeners:
        if port in ports:
            logger.error("%s and %s both request port %s", ports[port], name, port)
            return CONFIG_EXIT_CODE
        ports[port] = name
    blocked = _first_unbindable(listeners)
    if blocked is not None:
        logger.error(
            "%s port %s:%s is still unavailable; refusing a permanent restart loop",
            *blocked,
        )
        return CONFIG_EXIT_CODE

    children = _build_children(args)
    if not children:
        logger.error("no children to supervise (both --no-* flags set?)")
        return 2

    _write_pidfile()
    stop_event = threading.Event()

    def _shutdown(signum, _frame) -> None:
        logger.info("received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown)

    for c in children:
        c.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        for c in children:
            c.stop()
        _clear_pidfile()
        logger.info("brains-supervisor exited")
    return 0


def main() -> int:  # pragma: no cover - thin CLI entry point
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
