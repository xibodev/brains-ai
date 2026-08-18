"""macOS service backend — a launchd LaunchAgent.

A user LaunchAgent (under ``~/Library/LaunchAgents``) loads when the user
logs in and runs **as that user**, so HOME / OAuth / the canonical DB all
resolve correctly. ``RunAtLoad`` starts it at login; ``KeepAlive`` restarts
it on crash. The plist is rendered by a pure function so it can be unit-tested
on any host OS; registration shells out to ``launchctl``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from xml.sax.saxutils import escape

from brains.service.common import (
    LAUNCHD_LABEL,
    ServiceSpec,
    cleanup_stale_pidfile,
    read_pidfile_record,
    run_cmd,
    state_dir,
    verify_pid,
)


def plist_path() -> Path:
    """``~/Library/LaunchAgents/<label>.plist`` (honours a custom HOME)."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _log_paths() -> tuple[Path, Path]:
    sessions = state_dir() / "sessions"
    return sessions / "service.out.log", sessions / "service.err.log"


def render_plist(spec: ServiceSpec) -> str:
    """Render the LaunchAgent plist (``RunAtLoad`` + ``KeepAlive``)."""
    out_log, err_log = _log_paths()
    program_args = "".join(
        f"      <string>{escape(arg)}</string>\n" for arg in (spec.program, *spec.args)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{escape(LAUNCHD_LABEL)}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}    </array>
    <key>WorkingDirectory</key>
    <string>{escape(spec.working_dir)}</string>
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
    path = plist_path()
    report: dict = {
        "platform": "macos",
        "label": LAUNCHD_LABEL,
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
    # Reload cleanly: unload any prior copy, then load with -w (persisted).
    run_cmd(["launchctl", "unload", "-w", str(path)])
    rc, out, err = run_cmd(["launchctl", "load", "-w", str(path)])
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def uninstall(*, dry_run: bool = False) -> dict:
    path = plist_path()
    report: dict = {
        "platform": "macos",
        "label": LAUNCHD_LABEL,
        "action": "would-uninstall" if dry_run else "uninstall",
        "definition": str(path),
    }
    if dry_run:
        return report
    rc, out, err = run_cmd(["launchctl", "unload", "-w", str(path)])
    with contextlib.suppress(OSError):
        path.unlink()
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def start() -> dict:
    rc, out, err = run_cmd(["launchctl", "start", LAUNCHD_LABEL])
    return {"platform": "macos", "action": "start", "ok": rc == 0, "detail": out or err}


def stop() -> dict:
    """Stop via launchd, then signal the supervisor if its PID still checks out.

    A PID :func:`verify_pid` reports as ``stale`` (reused by an unrelated
    process since we last saw it) is never signalled by number alone; the
    stale pidfile is removed instead.
    """
    rc, out, err = run_cmd(["launchctl", "stop", LAUNCHD_LABEL])
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


def restart() -> dict:
    stopped = stop()
    if not stopped["ok"]:
        return {
            "platform": "macos",
            "action": "restart",
            "ok": False,
            "detail": f"restart refused because stop was incomplete: {stopped['detail']}",
        }
    return start()


def status() -> dict:
    rc, out, err = run_cmd(["launchctl", "list", LAUNCHD_LABEL])
    return {
        "platform": "macos",
        "label": LAUNCHD_LABEL,
        "installed": rc == 0,
        "state": "loaded" if rc == 0 else "not-loaded",
        "detail": out or err,
    }
