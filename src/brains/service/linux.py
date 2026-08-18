"""Linux service backend — a systemd ``--user`` unit.

A user unit (under ``~/.config/systemd/user``) runs **as the logged-in user**,
so HOME / OAuth / the canonical DB all resolve. ``WantedBy=default.target``
starts it at login; ``Restart=always`` restarts it on crash. We best-effort
enable *linger* so the unit can also come up at boot before an interactive
login (``loginctl enable-linger``) — failure there is non-fatal.

The unit file is rendered by a pure function so it can be unit-tested on any
host OS; registration shells out to ``systemctl --user``.
"""

from __future__ import annotations

import contextlib
import getpass
from pathlib import Path

from brains.service.common import (
    SYSTEMD_UNIT,
    ServiceSpec,
    run_cmd,
)


def unit_path() -> Path:
    """``~/.config/systemd/user/<unit>`` (honours a custom HOME)."""
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def render_unit(spec: ServiceSpec) -> str:
    """Render the systemd unit (``Restart=always`` + ``default.target``)."""
    exec_start = spec.command_line
    return f"""[Unit]
Description={spec.description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={spec.working_dir}
Restart=always
RestartSec=2
# Unlimited restarts: clear the default start-limit burst guard.
StartLimitIntervalSec=0

[Install]
WantedBy=default.target
"""


def _user_systemctl(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


def install(spec: ServiceSpec, *, dry_run: bool = False) -> dict:
    unit = render_unit(spec)
    path = unit_path()
    report: dict = {
        "platform": "linux",
        "label": SYSTEMD_UNIT,
        "action": "would-install" if dry_run else "install",
        "definition": str(path),
        "command": spec.command_line,
    }
    if dry_run:
        report["unit"] = unit
        return report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit, encoding="utf-8")
    run_cmd(_user_systemctl("daemon-reload"))
    # Best-effort linger so the service can start at boot before login.
    run_cmd(["loginctl", "enable-linger", getpass.getuser()])
    rc, out, err = run_cmd(_user_systemctl("enable", "--now", SYSTEMD_UNIT))
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def uninstall(*, dry_run: bool = False) -> dict:
    path = unit_path()
    report: dict = {
        "platform": "linux",
        "label": SYSTEMD_UNIT,
        "action": "would-uninstall" if dry_run else "uninstall",
        "definition": str(path),
    }
    if dry_run:
        return report
    rc, out, err = run_cmd(_user_systemctl("disable", "--now", SYSTEMD_UNIT))
    with contextlib.suppress(OSError):
        path.unlink()
    run_cmd(_user_systemctl("daemon-reload"))
    report["ok"] = rc == 0
    report["detail"] = out or err
    return report


def start() -> dict:
    rc, out, err = run_cmd(_user_systemctl("start", SYSTEMD_UNIT))
    return {"platform": "linux", "action": "start", "ok": rc == 0, "detail": out or err}


def stop() -> dict:
    rc, out, err = run_cmd(_user_systemctl("stop", SYSTEMD_UNIT))
    return {"platform": "linux", "action": "stop", "ok": rc == 0, "detail": out or err}


def restart() -> dict:
    rc, out, err = run_cmd(_user_systemctl("restart", SYSTEMD_UNIT))
    return {
        "platform": "linux",
        "action": "restart",
        "ok": rc == 0,
        "detail": out or err,
    }


def status() -> dict:
    rc, out, _ = run_cmd(_user_systemctl("is-active", SYSTEMD_UNIT))
    erc, _, _ = run_cmd(_user_systemctl("is-enabled", SYSTEMD_UNIT))
    return {
        "platform": "linux",
        "label": SYSTEMD_UNIT,
        "installed": erc == 0,
        "state": out or "inactive",
        "detail": out,
    }
