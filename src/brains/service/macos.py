"""macOS service backend — a launchd LaunchAgent.

A user LaunchAgent (under ``~/Library/LaunchAgents``) loads when the user
logs in and runs **as that user**, so HOME / OAuth / the canonical DB all
resolve correctly. ``RunAtLoad`` starts it at login; ``KeepAlive`` restarts
it on crash. The plist is rendered by a pure function so it can be unit-tested
on any host OS; registration shells out to ``launchctl``.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.sax.saxutils import escape

from brains.service.common import (
    SERVICE_LABEL,
    ServiceSpec,
    cleanup_stale_pidfile,
    native_service_identity,
    read_pidfile_record,
    run_cmd,
    state_dir,
    verify_pid,
)


def plist_path(label: str = SERVICE_LABEL) -> Path:
    """``~/Library/LaunchAgents/<label>.plist`` (honours a custom HOME)."""
    identity = native_service_identity("macos", label)
    return Path.home() / "Library" / "LaunchAgents" / f"{identity}.plist"


def _log_paths() -> tuple[Path, Path]:
    sessions = state_dir() / "sessions"
    return sessions / "service.out.log", sessions / "service.err.log"


def render_plist(spec: ServiceSpec) -> str:
    """Render the LaunchAgent plist (``RunAtLoad`` + ``KeepAlive``)."""
    out_log, err_log = _log_paths()
    identity = native_service_identity("macos", spec.label)
    program_args = "".join(
        f"      <string>{escape(arg)}</string>\n" for arg in (spec.program, *spec.args)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{escape(identity)}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}    </array>
    <key>WorkingDirectory</key>
    <string>{escape(spec.working_dir)}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>BRAINS_STATE_DIR</key>
      <string>{escape(spec.state_dir)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>{escape(str(out_log))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(err_log))}</string>
  </dict>
</plist>
"""


def install(spec: ServiceSpec, *, dry_run: bool = False) -> dict:
    plist = render_plist(spec)
    identity = native_service_identity("macos", spec.label)
    path = plist_path(spec.label)
    report: dict = {
        "platform": "macos",
        "label": identity,
        "action": "would-install" if dry_run else "install",
        "definition": str(path),
        "command": spec.command_line,
    }
    if dry_run:
        report["plist"] = plist
        return report
    path.parent.mkdir(parents=True, exist_ok=True)
    (state_dir() / "sessions").mkdir(parents=True, exist_ok=True)
    path.write_text(plist, encoding="utf-8")
    # Reload cleanly. bootout/bootstrap is the modern per-user contract;
    # unload/load remains a compatibility fallback on older supported macOS.
    _unload(spec.label)
    rc, out, err = _load(spec.label)
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def uninstall(*, dry_run: bool = False, label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("macos", label)
    path = plist_path(label)
    report: dict = {
        "platform": "macos",
        "label": identity,
        "action": "would-uninstall" if dry_run else "uninstall",
        "definition": str(path),
    }
    if dry_run:
        return report
    stopped = stop(label)
    rc = 0 if stopped["ok"] else 1
    out, err = (stopped["detail"], "") if stopped["ok"] else ("", stopped["detail"])
    if rc == 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            rc, err = 1, "native job was unloaded but its definition could not be removed"
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def _domain() -> str:
    # ``render_*`` and the stateful manager contract are intentionally tested
    # on every CI OS.  getuid exists on the actual supported macOS host but not
    # on Windows, where the command runner is synthetic.
    getuid = getattr(os, "getuid", lambda: 0)
    return f"gui/{getuid()}"


def _load(label: str) -> tuple[int, str, str]:
    path = plist_path(label)
    rc, out, err = run_cmd(["launchctl", "bootstrap", _domain(), str(path)])
    if rc == 0:
        return rc, out, err
    return run_cmd(["launchctl", "load", "-w", str(path)])


def _unload(label: str) -> tuple[int, str, str]:
    identity = native_service_identity("macos", label)
    rc, out, err = run_cmd(["launchctl", "bootout", f"{_domain()}/{identity}"])
    if rc == 0:
        return rc, out, err
    return run_cmd(["launchctl", "unload", "-w", str(plist_path(label))])


def start(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("macos", label)
    rc, out, err = run_cmd(["launchctl", "kickstart", "-k", f"{_domain()}/{identity}"])
    if rc != 0:
        rc, out, err = _load(label)
    return {"platform": "macos", "action": "start", "ok": rc == 0, "detail": out or err}


def stop(label: str = SERVICE_LABEL) -> dict:
    """Stop via launchd, then signal the supervisor if its PID still checks out.

    A PID :func:`verify_pid` reports as ``stale`` (reused by an unrelated
    process since we last saw it) is never signalled by number alone; the
    stale pidfile is removed instead.
    """
    rc, out, err = _unload(label)
    detail = out or err
    check = verify_pid(read_pidfile_record())
    pid = check["pid"]
    safe_cleanup = check["confidence"] in ("verified", "stale", "absent")
    termination_ok = True
    if pid is not None and check["confidence"] == "verified":
        krc, kout, kerr = run_cmd(["/bin/kill", "-TERM", str(pid)])
        termination_ok = krc == 0
        detail = f"{detail}; signal pid {pid}: {kout or kerr}".strip("; ")
    elif check["confidence"] in ("stale", "absent"):
        cleanup_stale_pidfile()
        if pid is not None:
            detail = f"{detail}; skipped signal: pid {pid} is stale ({check['reason']})".strip("; ")
    elif pid is not None:
        detail = (
            f"{detail}; refused signal: pid {pid} identity is "
            f"{check['confidence']} ({check['reason']})"
        ).strip("; ")
    return {
        "platform": "macos",
        "action": "stop",
        "ok": rc == 0 and safe_cleanup and termination_ok,
        "detail": detail,
    }


def restart(label: str = SERVICE_LABEL) -> dict:
    stopped = stop(label)
    if not stopped["ok"]:
        return {
            "platform": "macos",
            "action": "restart",
            "ok": False,
            "detail": f"restart refused because stop was incomplete: {stopped['detail']}",
        }
    return start(label)


def status(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("macos", label)
    rc, out, err = run_cmd(["launchctl", "print", f"{_domain()}/{identity}"])
    if rc != 0:
        rc, out, err = run_cmd(["launchctl", "list", identity])
    return {
        "platform": "macos",
        "label": identity,
        "installed": rc == 0,
        "state": "loaded" if rc == 0 else "not-loaded",
        "detail": out or err,
    }
