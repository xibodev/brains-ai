"""Linux service backend — a systemd ``--user`` unit.

A user unit (under ``~/.config/systemd/user``) runs **as the logged-in user**,
so HOME / OAuth / the canonical DB all resolve. ``WantedBy=default.target``
starts it at login; ``Restart=always`` restarts it on crash, except for the
supervisor's configuration/preflight exit code, which relaunching cannot fix.
Installation deliberately leaves the account's separate linger policy alone.

The unit file is rendered by a pure function so it can be unit-tested on any
host OS; registration shells out to ``systemctl --user``.
"""

from __future__ import annotations

from pathlib import Path

from brains.control.supervisor import CONFIG_EXIT_CODE
from brains.service.common import (
    SERVICE_LABEL,
    ServiceSpec,
    native_service_identity,
    run_cmd,
)


def unit_path(label: str = SERVICE_LABEL) -> Path:
    """``~/.config/systemd/user/<unit>`` (honours a custom HOME)."""
    return Path.home() / ".config" / "systemd" / "user" / native_service_identity("linux", label)


def render_unit(spec: ServiceSpec) -> str:
    """Render the systemd unit (``Restart=always`` + ``default.target``).

    ``RestartPreventExitStatus`` excludes the supervisor's configuration exit
    code so a port conflict it already refused is not relaunched forever.
    """
    exec_start = spec.command_line
    state_dir = spec.state_dir.replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description={spec.description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={spec.working_dir}
Environment="BRAINS_STATE_DIR={state_dir}"
Restart=always
RestartSec=2
# A configuration/preflight failure cannot be fixed by relaunching.
RestartPreventExitStatus={CONFIG_EXIT_CODE}
# Unlimited restarts: clear the default start-limit burst guard.
StartLimitIntervalSec=0

[Install]
WantedBy=default.target
"""


def _user_systemctl(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


def install(spec: ServiceSpec, *, dry_run: bool = False) -> dict:
    unit = render_unit(spec)
    identity = native_service_identity("linux", spec.label)
    path = unit_path(spec.label)
    report: dict = {
        "platform": "linux",
        "label": identity,
        "action": "would-install" if dry_run else "install",
        "definition": str(path),
        "command": spec.command_line,
    }
    if dry_run:
        report["unit"] = unit
        return report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit, encoding="utf-8")
    rrc, rout, rerr = run_cmd(_user_systemctl("daemon-reload"))
    if rrc != 0:
        report["ok"] = False
        report["detail"] = rout or rerr
        return report
    # Login persistence belongs to the user service target. Do not mutate the
    # account's separate, persistent linger policy as an installation side effect.
    rc, out, err = run_cmd(_user_systemctl("enable", "--now", identity))
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def uninstall(*, dry_run: bool = False, label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("linux", label)
    path = unit_path(label)
    report: dict = {
        "platform": "linux",
        "label": identity,
        "action": "would-uninstall" if dry_run else "uninstall",
        "definition": str(path),
    }
    if dry_run:
        return report
    try:
        definition = path.read_text(encoding="utf-8")
    except OSError:
        definition = None
    rc, out, err = run_cmd(_user_systemctl("disable", "--now", identity))
    if rc == 0:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            rc, err = 1, "native unit was disabled but its definition could not be removed"
    if rc == 0:
        rrc, rout, rerr = run_cmd(_user_systemctl("daemon-reload"))
        if rrc != 0:
            if definition is not None:
                # Keep the operator-visible definition available for a safe
                # diagnostic/retry when the manager could not converge.
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(definition, encoding="utf-8")
                except OSError:
                    rerr = f"{rerr}; local definition restoration failed".strip("; ")
            rc, out, err = rrc, rout, rerr
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def start(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("linux", label)
    rc, out, err = run_cmd(_user_systemctl("start", identity))
    return {"platform": "linux", "action": "start", "ok": rc == 0, "detail": out or err}


def stop(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("linux", label)
    rc, out, err = run_cmd(_user_systemctl("stop", identity))
    return {"platform": "linux", "action": "stop", "ok": rc == 0, "detail": out or err}


def restart(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("linux", label)
    rc, out, err = run_cmd(_user_systemctl("restart", identity))
    return {
        "platform": "linux",
        "action": "restart",
        "ok": rc == 0,
        "detail": out or err,
    }


def status(label: str = SERVICE_LABEL) -> dict:
    identity = native_service_identity("linux", label)
    rc, out, _ = run_cmd(_user_systemctl("is-active", identity))
    erc, _, _ = run_cmd(_user_systemctl("is-enabled", identity))
    return {
        "platform": "linux",
        "label": identity,
        "installed": erc == 0,
        "state": out or "inactive",
        "detail": out,
    }
