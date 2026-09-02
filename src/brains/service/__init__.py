"""Install ``brains serve-all`` as a user-level OS service.

Public API — all dispatch to the right backend for the current platform
(Windows Task Scheduler, macOS launchd, Linux systemd ``--user``):

    from brains import service
    service.install()      # register + start, autostart at next login
    service.status()
    service.stop() / service.start() / service.restart()
    service.uninstall()

Every backend renders its unit definition with a pure function
(``render_task_xml`` / ``render_plist`` / ``render_unit``) so the generated
artifact is unit-testable on any host OS; the install/uninstall/start/stop
verbs shell out to the platform's service manager.

The service always runs as the **logged-in user** (never root / LocalSystem)
and execs ``<python> -m brains serve-all`` — see ``brains.service.common``
for why both of those matter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from brains.service import linux, macos, windows
from brains.service.common import (
    ServiceSpec,
    UnsupportedPlatform,
    current_platform,
    default_spec,
    listener_status,
    read_pidfile_record,
    verify_pid,
    verify_service_interpreter,
    write_service_config,
)

_BACKENDS = {
    "windows": windows,
    "macos": macos,
    "linux": linux,
}


def _backend(platform: str | None = None) -> Any:
    plat = platform or current_platform()
    backend = _BACKENDS.get(plat)
    if backend is None:
        raise UnsupportedPlatform(
            f"No brains service backend for platform {plat!r}. "
            "Supported: windows (Task Scheduler), macos (launchd), "
            "linux (systemd --user)."
        )
    return backend


def supported() -> bool:
    """True when a service backend exists for the current platform."""
    return current_platform() in _BACKENDS


def install(
    spec: ServiceSpec | None = None,
    *,
    dry_run: bool = False,
    gateway_host: str = "127.0.0.1",
    gateway_port: int | None = None,
    mcp_port: int | None = None,
) -> dict:
    """Register (and, unless ``dry_run``, start) the autostart service."""
    backend = _backend()
    try:
        resolved = spec or default_spec(
            gateway_host=gateway_host,
            gateway_port=gateway_port,
            mcp_port=mcp_port,
            probe_default=not dry_run,
        )
    except ValueError as exc:
        return {"ok": False, "action": "refused", "detail": str(exc)}
    if current_platform() == "windows" and Path(resolved.program).name.casefold() != "pythonw.exe":
        return {
            "ok": False,
            "action": "refused",
            "detail": "Windows services require the same environment's windowless pythonw.exe",
        }
    check = verify_service_interpreter(resolved.program)
    if not check["ok"]:
        return {
            "ok": False,
            "action": "refused",
            "detail": "service interpreter cannot import brains",
            "interpreter": check,
        }
    report = backend.install(resolved, dry_run=dry_run)
    report["interpreter"] = check
    report["endpoints"] = {
        "console": f"http://{resolved.gateway_host}:{resolved.gateway_port}/app",
        "mcp": f"http://{resolved.gateway_host}:{resolved.mcp_port}/sse",
    }
    if not dry_run and report.get("ok"):
        report["config"] = str(write_service_config(resolved))
    return report


def uninstall(*, dry_run: bool = False) -> dict:
    """Stop and remove the autostart service."""
    return _backend().uninstall(dry_run=dry_run)


def start() -> dict:
    return _backend().start()


def stop() -> dict:
    return _backend().stop()


def restart() -> dict:
    return _backend().restart()


def status() -> dict:
    """Report whether the service is installed + its run state.

    ``report["service_pid"]`` is the :func:`brains.service.common.verify_pid`
    result for the supervisor pidfile — the OS-native install/enabled state
    above answers "is the service registered", this answers "does the PID
    it last recorded still name a live, matching process" (BL-P1-09). A
    stale/reused PID is reported, never silently treated as proof the
    service is running.
    """
    if not supported():
        return {
            "platform": current_platform(),
            "supported": False,
            "installed": False,
            "state": "unsupported",
            "service_pid": verify_pid(None),
        }
    report = _backend().status()
    report["supported"] = True
    report["service_pid"] = verify_pid(read_pidfile_record())
    report.update(listener_status())
    pid_confidence = report["service_pid"].get("confidence")
    installed = bool(report.get("installed"))
    protocol_ready = bool(report["mcp_protocol"].get("ready"))
    mcp_listener = bool(report["listeners"].get("mcp"))
    if installed and pid_confidence == "verified":
        runtime_classification = (
            "installed-owned-ready" if protocol_ready else "installed-owned-unready"
        )
    elif pid_confidence == "stale":
        runtime_classification = "stale-pid"
    elif mcp_listener and (installed or pid_confidence not in {"absent", None}):
        runtime_classification = "unknown-port-owner"
    elif protocol_ready:
        runtime_classification = "manual-running"
    elif mcp_listener:
        runtime_classification = "unknown-port-owner"
    else:
        runtime_classification = "stopped"
    report["runtime_classification"] = runtime_classification
    report["healthy"] = bool(
        installed and pid_confidence == "verified" and report["serving"]
    )
    return report


def render_definition(spec: ServiceSpec | None = None) -> str:
    """Return the unit-definition text the current platform would install."""
    spec = spec or default_spec()
    plat = current_platform()
    if plat == "windows":
        return windows.render_task_xml(spec)
    if plat == "macos":
        return macos.render_plist(spec)
    if plat == "linux":
        return linux.render_unit(spec)
    raise UnsupportedPlatform(f"No brains service backend for platform {plat!r}.")


__all__ = [
    "ServiceSpec",
    "UnsupportedPlatform",
    "current_platform",
    "default_spec",
    "install",
    "listener_status",
    "read_pidfile_record",
    "render_definition",
    "restart",
    "start",
    "status",
    "stop",
    "supported",
    "uninstall",
    "verify_pid",
    "verify_service_interpreter",
]
