"""macOS service backend — a launchd LaunchAgent.

A user LaunchAgent (under ``~/Library/LaunchAgents``) loads when the user
logs in and runs **as that user**, so HOME / OAuth / the canonical DB all
resolve correctly. ``RunAtLoad`` starts it at login; ``KeepAlive`` restarts
it on crash. The plist is rendered by a pure function so it can be unit-tested
on any host OS; registration shells out to ``launchctl``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from xml.sax.saxutils import escape

from brains.service.common import (
    SERVICE_LABEL,
    ServiceSpec,
    cleanup_stale_pidfile,
    default_pidfile_path,
    native_service_identity,
    read_pidfile_record,
    run_cmd,
    state_dir,
    verify_pid,
)

# Two sequential child stops can each wait 10s for exit and 15s for their thread.
_STOP_TIMEOUT_SECONDS = 60.0
_STOP_POLL_SECONDS = 0.2


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
    report["error_code"] = stopped.get("error_code")
    for key in ("pid_confidence", "pid_identity_evidence"):
        if key in stopped:
            report[key] = stopped[key]
    rc = 0 if stopped["ok"] else 1
    out, err = (stopped["detail"], "") if stopped["ok"] else ("", stopped["detail"])
    if rc == 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            rc, out, err = 1, "", "native job was unloaded but its definition could not be removed"
            report["error_code"] = "definition-cleanup-incomplete"
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
    rc, out, err = run_cmd(["launchctl", "kickstart", f"{_domain()}/{identity}"])
    if rc != 0:
        rc, out, err = _load(label)
    return {"platform": "macos", "action": "start", "ok": rc == 0, "detail": out or err}


def stop(label: str = SERVICE_LABEL) -> dict:
    """Unload, signal only the recorded process instance, and wait for its exit."""
    # Keep the identity even if launchd shutdown removes the file before exit.
    try:
        captured_content = default_pidfile_path().read_bytes()
    except OSError:
        captured_content = b""
    record = read_pidfile_record()
    captured_verified = verify_pid(record)["confidence"] == "verified"
    rc, out, err = _unload(label)
    detail = out or err
    check = verify_pid(record)
    if rc != 0:
        # A failed targeted query cannot distinguish absence from manager failure.
        # Only a successful complete listing can establish an already-unloaded job.
        absent = False
        if check["confidence"] in ("stale", "absent") and not check["running"]:
            lrc, listing, _ = run_cmd(["launchctl", "list"])
            rows = [line.split() for line in listing.splitlines() if line.strip()]
            identity = native_service_identity("macos", label)
            absent = (
                lrc == 0
                and bool(rows)
                and rows[0] == ["PID", "Status", "Label"]
                and all(len(row) == 3 and row[2] != identity for row in rows[1:])
            )
        if not absent:
            return {
                "platform": "macos",
                "action": "stop",
                "ok": False,
                "detail": detail,
                "error_code": "native-unload-failed",
            }
        detail = f"{detail}; native job already absent".strip("; ")
        check = verify_pid(record)
    pid = check["pid"]
    error_code = None
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    krc = None
    while rc == 0 and pid is not None:
        current = read_pidfile_record()
        if current != record and (current is not None or default_pidfile_path().exists()):
            return {
                "platform": "macos",
                "action": "stop",
                "ok": False,
                "detail": f"{detail}; pidfile changed during stop; retained for review".strip("; "),
                "error_code": "pidfile-changed",
            }
        if not check["running"] or check.get("identity_mismatch") is True:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if check["confidence"] == "verified" and krc is None:
            check = verify_pid(record)
            if check["confidence"] != "verified":
                continue
            krc, kout, kerr = run_cmd(["/bin/kill", "-TERM", str(pid)])
            detail = f"{detail}; signal pid {pid}: {kout or kerr}".strip("; ")
            check = verify_pid(record)
            continue
        if check["confidence"] == "verified" and krc != 0:
            break
        # Missing fields or a one-field mismatch do not prove exit/PID reuse.
        # Wait read-only while uncertain; never signal using the earlier identity.
        time.sleep(min(_STOP_POLL_SECONDS, remaining))
        check = verify_pid(record)

    reused_after_unload = rc == 0 and captured_verified and check.get("identity_mismatch") is True
    stopped = (
        check["confidence"] in ("stale", "absent") and not check["running"]
    ) or reused_after_unload
    if stopped:
        # Do not follow or discard a replacement supervisor's PID record.
        current = read_pidfile_record()
        if current != record and (current is not None or default_pidfile_path().exists()):
            stopped = False
            error_code = "pidfile-changed"
            detail = f"{detail}; pidfile changed during stop; retained for review".strip("; ")
        else:
            cleanup = cleanup_stale_pidfile(
                expected_content=captured_content,
                allow_identity_mismatch=reused_after_unload,
            )
            stopped = (
                cleanup["confidence"] in ("stale", "absent")
                and (
                    not cleanup["running"]
                    or (reused_after_unload and cleanup.get("identity_mismatch") is True)
                )
                and not default_pidfile_path().exists()
            )
            detail = f"{detail}; pid {pid}: {check['confidence']} ({check['reason']})".strip("; ")
            if not stopped:
                error_code = "pidfile-cleanup-incomplete"
                detail = f"{detail}; pidfile cleanup incomplete"
    elif check["confidence"] == "verified":
        error_code = "pid-still-running"
        detail = f"{detail}; stop incomplete: owned pid {pid} has not exited".strip("; ")
    else:
        error_code = "pid-identity-unsafe"
        detail = (
            f"{detail}; refused signal: pid {pid} identity is "
            f"{check['confidence']} ({check['reason']})"
        ).strip("; ")
    return {
        "platform": "macos",
        "action": "stop",
        "ok": stopped,
        "detail": detail,
        "error_code": error_code,
        "pid_confidence": check["confidence"],
        "pid_identity_evidence": (
            "executable-and-start-time-mismatch"
            if check.get("identity_mismatch") is True
            else "process-absent"
            if check["confidence"] == "stale" and not check["running"]
            else "identity-not-proven"
            if check["confidence"] in ("stale", "degraded", "unverified")
            else "identity-matching"
            if check["confidence"] == "verified"
            else "no-record"
        ),
    }


def restart(label: str = SERVICE_LABEL) -> dict:
    stopped = stop(label)
    if not stopped["ok"]:
        return {
            "platform": "macos",
            "action": "restart",
            "ok": False,
            "detail": f"restart refused because stop was incomplete: {stopped['detail']}",
            "error_code": stopped.get("error_code", "stop-incomplete"),
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
