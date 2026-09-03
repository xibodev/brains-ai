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

import csv
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


def definition_path(label: str = SERVICE_LABEL) -> Path:
    """Where we stash the rendered XML (for reference + idempotency)."""
    return state_dir() / "service" / f"{native_service_identity('windows', label)}.xml"


def render_task_xml(spec: ServiceSpec) -> str:
    """Render a Task Scheduler 1.2 XML document for the serve-all task.

    Encodes: LogonTrigger for ``spec.user``; an interactive-token principal at
    least-privilege; restart-on-failure (every 1 minute, up to 9999 times); no
    execution time limit; hidden; single-instance; and the verified windowless
    ``pythonw -m brains serve-all`` action with a neutral working directory.
    """
    arguments = " ".join(spec.args)
    user = escape(spec.user)
    task_name = native_service_identity("windows", spec.label)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(spec.description)}</Description>
    <URI>\\{task_name}</URI>
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
    task_name = native_service_identity("windows", spec.label)
    path = definition_path(spec.label)
    register = ["schtasks", "/Create", "/TN", task_name, "/XML", str(path), "/F"]
    report: dict = {
        "platform": "windows",
        "label": task_name,
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
        started = start(spec.label)
        report["started"] = started["ok"]
        report["start_detail"] = started["detail"]
        report["ok"] = bool(report["ok"] and started["ok"])
    return report


def uninstall(*, dry_run: bool = False, label: str = SERVICE_LABEL) -> dict:
    task_name = native_service_identity("windows", label)
    cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    report: dict = {
        "platform": "windows",
        "label": task_name,
        "action": "would-uninstall" if dry_run else "uninstall",
        "cmd": " ".join(cmd),
    }
    if dry_run:
        return report
    stopped = stop(label)
    if not stopped["ok"]:
        report["ok"] = False
        report["detail"] = f"uninstall refused because stop was incomplete: {stopped['detail']}"
        return report
    rc, out, err = run_cmd(cmd)
    report["ok"] = rc == 0
    report["detail"] = out or err
    if report["ok"]:
        removed, removal_detail = _delete_definition(label)
        if not removed:
            report["ok"] = False
            report["detail"] = f"{report['detail']}; {removal_detail}".strip("; ")
    return report


def _delete_definition(label: str = SERVICE_LABEL) -> tuple[bool, str]:
    try:
        definition_path(label).unlink()
    except FileNotFoundError:
        return True, ""
    except OSError:
        return False, "native task was removed but its local definition could not be removed"
    return True, ""


def start(label: str = SERVICE_LABEL) -> dict:
    task_name = native_service_identity("windows", label)
    rc, out, err = run_cmd(["schtasks", "/Run", "/TN", task_name])
    return {"platform": "windows", "action": "start", "ok": rc == 0, "detail": out or err}


def stop(label: str = SERVICE_LABEL) -> dict:
    """End the task, then reap the supervisor tree.

    ``schtasks /End`` only stops the task's action process; the gateway /
    dashboard / MCP children it spawned are orphaned. We additionally kill the
    process tree rooted at the supervisor PID recorded in ``service.pid`` -
    but only after :func:`verify_pid` confirms that PID still names the
    supervisor we recorded. A PID the OS has since reused for an unrelated
    process (``stale``) is never tree-killed by number alone; the stale
    pidfile is removed instead so a future ``status()`` stops trusting it.
    """
    task_name = native_service_identity("windows", label)
    rc, out, err = run_cmd(["schtasks", "/End", "/TN", task_name])
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


def restart(label: str = SERVICE_LABEL) -> dict:
    stopped = stop(label)
    if not stopped["ok"]:
        return {
            "platform": "windows",
            "action": "restart",
            "ok": False,
            "detail": f"restart refused because stop was incomplete: {stopped['detail']}",
        }
    return start(label)


def status(label: str = SERVICE_LABEL) -> dict:
    # Parse CSV (fixed column order) rather than the localized LIST output:
    # /FO LIST labels ("Status:" / "Estado:") differ per UI language, but the
    # CSV column positions are stable — TaskName, Next Run Time, Status.
    task_name = native_service_identity("windows", label)
    rc, out, err = run_cmd(["schtasks", "/Query", "/TN", task_name, "/FO", "CSV", "/NH"])
    installed = rc == 0
    state = "unknown"
    if installed and out:
        row = next(csv.reader([out.splitlines()[0]]), [])
        if len(row) >= 3:
            state = row[2]  # raw (may be localized, e.g. "Ready" / "Listo")
    return {
        "platform": "windows",
        "label": task_name,
        "installed": installed,
        "state": state,
        "detail": out or err,
    }
