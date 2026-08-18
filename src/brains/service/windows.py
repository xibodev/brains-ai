"""Windows service backend — a Task Scheduler task.

Why a Scheduled Task and not a true Windows Service? A real service requires
either a service-aware binary (the SCM kills plain executables that don't
answer its control protocol) or a third-party wrapper like NSSM. A Scheduled
Task is dependency-free, ships with every Windows install, and supports
everything we need: start at logon, run hidden, restart on failure, and run
**as the logged-in user** (an interactive-token principal, so HOME / OAuth /
the canonical DB all resolve and no password is stored).

The task XML is rendered by a pure function (:func:`render_task_xml`) so it
can be unit-tested on any host OS; registration shells out to ``schtasks``.
"""

from __future__ import annotations

import contextlib
import csv
from pathlib import Path
from xml.sax.saxutils import escape

from brains.service.common import (
    WINDOWS_TASK_NAME,
    ServiceSpec,
    cleanup_stale_pidfile,
    read_pidfile_record,
    run_cmd,
    state_dir,
    verify_pid,
)


def definition_path() -> Path:
    """Where we stash the rendered XML (for reference + idempotency)."""
    return state_dir() / "service" / f"{WINDOWS_TASK_NAME}.xml"


def render_task_xml(spec: ServiceSpec) -> str:
    """Render a Task Scheduler 1.2 XML document for the serve-all task.

    Encodes: LogonTrigger for ``spec.user``; an interactive-token principal at
    least-privilege; restart-on-failure (every 1 minute, up to 9999 times); no
    execution time limit; hidden; single-instance; and the ``pythonw -m brains
    serve-all`` action with a neutral working directory.
    """
    arguments = " ".join(spec.args)
    user = escape(spec.user)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(spec.description)}</Description>
    <URI>\\{WINDOWS_TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>9999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(spec.program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(spec.working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install(spec: ServiceSpec, *, dry_run: bool = False) -> dict:
    xml = render_task_xml(spec)
    path = definition_path()
    register = ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(path), "/F"]
    report: dict = {
        "platform": "windows",
        "label": WINDOWS_TASK_NAME,
        "action": "would-install" if dry_run else "install",
        "definition": str(path),
        "command": spec.command_line,
        "register_cmd": " ".join(register),
    }
    if dry_run:
        report["xml"] = xml
        return report
    path.parent.mkdir(parents=True, exist_ok=True)
    # schtasks /XML wants a UTF-16 document; match the XML declaration.
    path.write_text(xml, encoding="utf-16")
    rc, out, err = run_cmd(register)
    report["ok"] = rc == 0
    report["detail"] = out or err
    # schtasks only arms the logon trigger; start it now so `install` brings
    # the stack up immediately (parity with launchd RunAtLoad / systemd --now).
    if report["ok"]:
        started = start()
        report["started"] = started["ok"]
        report["start_detail"] = started["detail"]
    return report


def uninstall(*, dry_run: bool = False) -> dict:
    cmd = ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"]
    report: dict = {
        "platform": "windows",
        "label": WINDOWS_TASK_NAME,
        "action": "would-uninstall" if dry_run else "uninstall",
        "cmd": " ".join(cmd),
    }
    if dry_run:
        return report
    rc, out, err = run_cmd(cmd)
    report["ok"] = rc == 0
    report["detail"] = out or err
    _delete_definition()
    return report


def _delete_definition() -> None:
    with contextlib.suppress(OSError):
        definition_path().unlink()


def start() -> dict:
    rc, out, err = run_cmd(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
    return {"platform": "windows", "action": "start", "ok": rc == 0, "detail": out or err}


def stop() -> dict:
    """End the task, then reap the supervisor tree.

    ``schtasks /End`` only stops the task's action process; the gateway /
    dashboard / MCP children it spawned are orphaned. We additionally kill the
    process tree rooted at the supervisor PID recorded in ``service.pid`` -
    but only after :func:`verify_pid` confirms that PID still names the
    supervisor we recorded. A PID the OS has since reused for an unrelated
    process (``stale``) is never tree-killed by number alone; the stale
    pidfile is removed instead so a future ``status()`` stops trusting it.
    """
    rc, out, err = run_cmd(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
    detail = out or err
    check = verify_pid(read_pidfile_record())
    pid = check["pid"]
    safe_cleanup = check["confidence"] in ("verified", "stale", "absent")
    termination_ok = True
    if pid is not None and check["confidence"] == "verified":
        krc, kout, kerr = run_cmd(["taskkill", "/PID", str(pid), "/T", "/F"])
        termination_ok = krc == 0
        detail = f"{detail}; tree-kill pid {pid}: {kout or kerr}".strip("; ")
    elif check["confidence"] in ("stale", "absent"):
        cleanup_stale_pidfile()
        if pid is not None:
            detail = f"{detail}; skipped tree-kill: pid {pid} is stale ({check['reason']})".strip(
                "; "
            )
    elif pid is not None:
        detail = (
            f"{detail}; refused tree-kill: pid {pid} identity is "
            f"{check['confidence']} ({check['reason']})"
        ).strip("; ")
    return {
        "platform": "windows",
        "action": "stop",
        "ok": rc == 0 and safe_cleanup and termination_ok,
        "detail": detail,
    }


def restart() -> dict:
    stopped = stop()
    if not stopped["ok"]:
        return {
            "platform": "windows",
            "action": "restart",
            "ok": False,
            "detail": f"restart refused because stop was incomplete: {stopped['detail']}",
        }
    return start()


def status() -> dict:
    # Parse CSV (fixed column order) rather than the localized LIST output:
    # /FO LIST labels ("Status:" / "Estado:") differ per UI language, but the
    # CSV column positions are stable — TaskName, Next Run Time, Status.
    rc, out, err = run_cmd(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME, "/FO", "CSV", "/NH"])
    installed = rc == 0
    state = "unknown"
    if installed and out:
        row = next(csv.reader([out.splitlines()[0]]), [])
        if len(row) >= 3:
            state = row[2]  # raw (may be localized, e.g. "Ready" / "Listo")
    return {
        "platform": "windows",
        "label": WINDOWS_TASK_NAME,
        "installed": installed,
        "state": state,
        "detail": out or err,
    }
