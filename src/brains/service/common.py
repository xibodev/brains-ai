"""Shared types + helpers for the OS-service installers.

``brains.service`` installs the supervised ``serve-all`` stack as a
user-level OS service that starts at login and restarts on failure:

* **Windows** — a Task Scheduler task (``brains.service.windows``).
* **macOS**   — a launchd LaunchAgent (``brains.service.macos``).
* **Linux**   — a systemd ``--user`` unit (``brains.service.linux``).

Two hard rules, learned the hard way, are encoded here:

1. **Run as the logged-in user, never root / LocalSystem.** brains resolves
   its state dir, the canonical per-machine DB, and the ``github_copilot``
   OAuth cache from the user's ``HOME``. A system-account service would point
   ``HOME`` elsewhere — breaking auth and silently re-fragmenting the DB into
   a second per-profile ``brains.db``.
2. **Exec via ``<python> -m brains serve-all``.** This is the most portable
   launch form: it works from the same interpreter that has brains installed
   (the pipx/uv venv python running the ``install`` command) without relying
    on the ``brains-ai`` console script being on ``PATH``. Use the exact
    ``sys.executable`` that passed the install command; sibling ``pythonw.exe``
    launchers from some uv-created environments do not resolve the venv.
"""

from __future__ import annotations

import contextlib
import csv
import getpass
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Logical identifiers, mapped to per-OS names by each backend.
SERVICE_LABEL = "brains-serve-all"
WINDOWS_TASK_NAME = "BrainsServeAll"
LAUNCHD_LABEL = "com.brains.serve-all"
SYSTEMD_UNIT = "brains-serve-all.service"

SERVICE_DESCRIPTION = (
    "Brains control plane — supervises the gateway (127.0.0.1:8787), the "
    "MCP SSE server (127.0.0.1:9877), and opt-in experimental children. "
    "Starts at login and restarts on failure."
)


def current_platform() -> str:
    """Return ``'windows'``, ``'macos'``, ``'linux'``, or the raw platform."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def state_dir() -> Path:
    """The brains state dir (``BRAINS_STATE_DIR`` or ``~/.brains``)."""
    override = os.environ.get("BRAINS_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".brains").resolve()


def _service_python(executable: str | None = None) -> str:
    """The interpreter the service should launch.

    Defaults to the exact interpreter running this process — i.e. the venv
    that has brains installed. Never guess a sibling launcher.
    """
    return str(Path(executable or sys.executable))


def verify_service_interpreter(program: str) -> dict[str, Any]:
    """Prove the service interpreter imports this installed Brains package."""
    rc, out, err = run_cmd(
        [
            program,
            "-c",
            "import brains,sys; print(sys.prefix); print(brains.__file__)",
        ]
    )
    return {
        "ok": rc == 0,
        "program": program,
        "detail": out or err,
    }


def listener_status(host: str = "127.0.0.1") -> dict[str, Any]:
    """Bounded probes for the default supervised listeners."""
    import socket

    listeners: dict[str, bool] = {}
    for name, port in (("gateway", 8787), ("mcp", 9877)):
        try:
            with socket.create_connection((host, port), timeout=0.4):
                listeners[name] = True
        except OSError:
            listeners[name] = False
    return {
        "listeners": listeners,
        "serving": all(listeners.values()),
    }


def current_user() -> str:
    """Best-effort ``DOMAIN\\user`` (Windows) or bare username (POSIX)."""
    name = getpass.getuser()
    if current_platform() == "windows":
        domain = os.environ.get("USERDOMAIN")
        if domain and "\\" not in name:
            return f"{domain}\\{name}"
    return name


@dataclass
class ServiceSpec:
    """Everything a backend needs to render + register the service.

    Pure data — backends turn this into a Task Scheduler XML / launchd plist /
    systemd unit. Construct via :func:`default_spec` for the live values.
    """

    program: str
    args: list[str] = field(default_factory=lambda: ["-m", "brains", "serve-all"])
    working_dir: str = field(default_factory=lambda: str(Path.home()))
    user: str = field(default_factory=current_user)
    label: str = SERVICE_LABEL
    description: str = SERVICE_DESCRIPTION
    state_dir: str = field(default_factory=lambda: str(state_dir()))

    @property
    def command_line(self) -> str:
        """``program + args`` as a display string (quoted where needed)."""
        parts = [self.program, *self.args]
        return " ".join(_quote(p) for p in parts)


def _quote(token: str) -> str:
    return f'"{token}"' if (" " in token and not token.startswith('"')) else token


def default_spec(executable: str | None = None) -> ServiceSpec:
    """Build a :class:`ServiceSpec` from the live interpreter + environment."""
    return ServiceSpec(program=_service_python(executable))


def run_cmd(cmd: list[str], *, check: bool = False) -> tuple[int, str, str]:
    """Run ``cmd`` and return ``(returncode, stdout, stderr)``.

    Never raises on a non-zero exit or a missing platform utility unless
    ``check`` is set; callers fold the result into a structured report instead.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - args are constructed, never shell
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        if check:
            raise RuntimeError(f"command could not start: {' '.join(cmd)}: {exc}") from exc
        return 127, "", str(exc)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def default_pidfile_path() -> Path:
    """The supervisor's own PID file: ``<state>/sessions/service.pid``."""
    return state_dir() / "sessions" / "service.pid"


def read_pidfile(path: Path | None = None) -> int | None:
    """Return the PID recorded at ``path`` (default: ``service.pid``).

    Legacy int-only accessor, kept for existing callers (the Windows/macOS
    ``stop()`` tree-kill paths). Prefer :func:`read_pidfile_record` +
    :func:`verify_pid` for anything that reports or acts on *whether the
    service is actually running* — a bare PID number proves nothing (BL-P1-09).
    """
    record = read_pidfile_record(path)
    if record is None:
        return None
    pid = record.get("pid")
    return pid if isinstance(pid, int) else None


# --------------------------------------------------------------------------- #
# PID identity — BL-P1-09
#
# A PID file historically held nothing but a bare integer: no proof the
# number still names *our* process rather than an unrelated one the OS
# recycled the PID onto after a crash or reboot. ``write_pidfile`` now
# additively records the executable path, a command-line fingerprint, and
# (where the platform exposes it) the process start time, captured for the
# same PID right after we launched it. ``verify_pid`` compares that record
# against the live process table so ``status()``/``stop()`` can distinguish
# "running", "stale" (PID reused by something else), and "unverified"
# (a legacy plain-integer file, or a platform that can't expose identity) —
# never confidently reporting a bare number as proof of liveness.
# --------------------------------------------------------------------------- #

#: Bumped only if the on-disk shape changes in a backwards-incompatible way.
PIDFILE_FORMAT = 2

#: Confidence levels returned by :func:`verify_pid`.
CONFIDENCE_ABSENT = "absent"  # no pidfile / no recorded pid
CONFIDENCE_STALE = "stale"  # recorded pid does not name a live matching process
CONFIDENCE_UNVERIFIED = "unverified"  # legacy pidfile: alive, but nothing to compare
CONFIDENCE_DEGRADED = "degraded"  # alive, but identity could not be confirmed here
CONFIDENCE_VERIFIED = "verified"  # alive AND executable/start-time match

# Allow a couple of seconds of slack when comparing recorded vs. live process
# start times — WMI/`/proc`/`ps` each round to a different granularity.
_START_TIME_TOLERANCE_SECONDS = 2.0


def _quote_cmdline(argv: list[str]) -> str:
    return " ".join(_quote(str(a)) for a in argv)


def _normalize_exe(value: str | None) -> str | None:
    """Basename, lower-cased — robust to short image names vs. full paths
    (``tasklist`` reports ``python.exe``; ``sys.executable`` is the full path)."""
    if not value:
        return None
    cleaned = value.strip().strip('"')
    if not cleaned:
        return None
    return Path(cleaned).name.lower()


def _linux_identity(pid: int) -> dict[str, Any] | None:
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.is_dir():
        return None
    exe: str | None = None
    with contextlib.suppress(OSError):
        exe = os.readlink(proc_dir / "exe")
    start_time: float | None = None
    with contextlib.suppress(OSError, ValueError, IndexError):
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        # ``comm`` (field 2) may itself contain spaces/parens; split after its
        # closing paren so the remaining fields line up. Field 22 (starttime)
        # is then at offset 19 into the remainder (fields 3..).
        after = stat_text.rsplit(")", 1)[-1].split()
        ticks = int(after[19])
        hz = cast(Any, os).sysconf("SC_CLK_TCK")
        btime: int | None = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is not None:
            start_time = float(btime) + ticks / hz
    return {"exe": exe, "start_time": start_time}


def _macos_identity(pid: int) -> dict[str, Any] | None:
    rc, out, _ = run_cmd(["ps", "-p", str(pid), "-o", "pid="])
    if rc != 0 or not out.strip():
        return None
    exe: str | None = None
    rc2, out2, _ = run_cmd(["ps", "-p", str(pid), "-o", "comm="])
    if rc2 == 0 and out2.strip():
        exe = out2.strip()
    start_time: float | None = None
    rc3, out3, _ = run_cmd(["ps", "-p", str(pid), "-o", "lstart="])
    if rc3 == 0 and out3.strip():
        with contextlib.suppress(ValueError):
            start_time = time.mktime(time.strptime(out3.strip(), "%a %b %d %H:%M:%S %Y"))
    return {"exe": exe, "start_time": start_time}


def _parse_wmi_datetime(value: str) -> float | None:
    """Parse a WMI ``CIM_DATETIME`` (``yyyymmddHHMMSS.ffffff+UUU``) to epoch."""
    body = value.strip()
    if len(body) < 14:
        return None
    try:
        dt = datetime.strptime(body[:14], "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=None).timestamp()
    except ValueError:
        return None


def _windows_identity(pid: int) -> dict[str, Any] | None:
    rc, out, _ = run_cmd(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if rc != 0 or not out or out.strip().lower().startswith("info:"):
        return None
    row = next(csv.reader([out.splitlines()[0]]), [])
    if not row:
        return None
    exe: str | None = row[0] or None  # short image name, e.g. "python.exe"
    start_time: float | None = None
    command_line: str | None = None
    # Prefer the supported CIM API through PowerShell. Windows PowerShell and
    # PowerShell 7 both expose Get-CimInstance on supported hosts.
    with contextlib.suppress(Exception):
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
            "if($p){$p|Select-Object ExecutablePath,CommandLine,"
            "@{n='CreationDate';e={$_.CreationDate.ToUniversalTime().ToString('o')}}"
            "|ConvertTo-Json -Compress}"
        )
        rc_cim, out_cim, _ = run_cmd(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        if rc_cim == 0 and out_cim.strip():
            data = json.loads(out_cim)
            if isinstance(data, dict):
                path = data.get("ExecutablePath")
                if isinstance(path, str) and path.strip():
                    exe = path.strip()
                command = data.get("CommandLine")
                if isinstance(command, str) and command.strip():
                    command_line = command.strip()
                created = data.get("CreationDate")
                if isinstance(created, str) and created.strip():
                    with contextlib.suppress(ValueError):
                        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        start_time = parsed.timestamp()
    # Best-effort full path + creation time. `wmic` is deprecated on newer
    # Windows builds; retain it only as a compatibility fallback.
    if start_time is None:
        with contextlib.suppress(Exception):
            rc2, out2, _ = run_cmd(
                [
                    "wmic",
                    "process",
                    "where",
                    f"ProcessId={pid}",
                    "get",
                    "ExecutablePath,CreationDate",
                    "/FORMAT:LIST",
                ]
            )
            if rc2 == 0 and out2:
                for line in out2.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("ExecutablePath="):
                        value = stripped.split("=", 1)[1].strip()
                        if value:
                            exe = value
                    elif stripped.startswith("CreationDate="):
                        start_time = _parse_wmi_datetime(stripped.split("=", 1)[1])
    return {"exe": exe, "start_time": start_time, "cmdline": command_line}


def _read_process_identity(pid: int) -> dict[str, Any] | None:
    """Best-effort live identity for ``pid``: ``{"exe": ..., "start_time": ...}``.

    Returns ``None`` when no process with this PID is currently running.
    Individual fields may be ``None`` when this platform/permission level
    can't expose them — callers must treat a ``None`` field as "not
    comparable", never as a mismatch.
    """
    plat = current_platform()
    try:
        if plat == "linux":
            return _linux_identity(pid)
        if plat == "macos":
            return _macos_identity(pid)
        if plat == "windows":
            return _windows_identity(pid)
    except Exception:  # pragma: no cover - defensive; never raise from a probe
        return None
    return None


def write_pidfile(
    path: Path | None = None,
    *,
    pid: int | None = None,
    cmdline: list[str] | None = None,
) -> dict[str, Any]:
    """Write the additive PID-identity file (default: ``service.pid``).

    Captures the PID plus, where the platform allows it, the executable and
    process start time recorded for that same PID right after launch — so a
    later :func:`verify_pid` has something to compare the live process
    against instead of trusting the bare number.
    """
    resolved_pid = pid if pid is not None else os.getpid()
    identity = _read_process_identity(resolved_pid) or {}
    record: dict[str, Any] = {
        "format": PIDFILE_FORMAT,
        "pid": resolved_pid,
        "exe": identity.get("exe") or sys.executable,
        "cmdline": _quote_cmdline(cmdline if cmdline is not None else sys.argv),
        "start_time": identity.get("start_time"),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    target = path or default_pidfile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record), encoding="utf-8")
    return record


def read_pidfile_record(path: Path | None = None) -> dict[str, Any] | None:
    """Read a PID-identity file, tolerating the legacy plain-integer format.

    Returns ``None`` when the file is absent or unreadable. A legacy file
    (a bare integer, written by a build predating BL-P1-09) is returned as
    ``{"format": "legacy", "pid": <int>, "exe": None, ...}`` so callers can
    still recover the PID, while :func:`verify_pid` treats it as
    unverifiable rather than confidently running.
    """
    target = path or default_pidfile_path()
    try:
        raw = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    legacy_record: dict[str, Any] | None = None
    try:
        pid = int(raw)
    except ValueError:
        pid = None
    if pid is not None:
        legacy_record = {
            "format": "legacy",
            "pid": pid,
            "exe": None,
            "cmdline": None,
            "start_time": None,
            "recorded_at": None,
        }
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return legacy_record
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        # A bare integer is valid JSON too (``json.loads("4242") == 4242``);
        # anything that isn't the structured ``{"pid": ...}`` shape falls
        # back to the legacy bare-integer interpretation.
        return legacy_record
    return data


def verify_pid(record: dict[str, Any] | int | None) -> dict[str, Any]:
    """Validate a recorded PID against the live process table.

    ``record`` is whatever :func:`read_pidfile_record` returned (or a bare
    ``int`` for callers that only have that). Returns::

        {
          "pid": int | None,
          "running": bool,                 # a process with this PID exists now
          "identity_verified": bool | None,  # None when unverifiable
          "confidence": "absent" | "stale" | "unverified" | "degraded" | "verified",
          "reason": str,
        }

    * ``absent``     — no PID recorded at all.
    * ``stale``       — a PID was recorded, but either no live process has
      that PID, or one does and its executable/start time contradict what
      was recorded — almost certainly a reused PID, not our process.
    * ``unverified``  — a legacy plain-integer file: a live process has that
      PID, but there is no recorded identity to compare it against.
    * ``degraded``    — a live process has that PID and some identity was
      recorded, but this platform/permission level could not confirm a
      match on either field — never confidently "running".
    * ``verified``    — a live process has that PID AND its recorded process
      start time matches; executable identity, when available, matches too.
    """
    if isinstance(record, int):
        record = {"format": "legacy", "pid": record, "exe": None, "start_time": None}
    if record is None:
        return {
            "pid": None,
            "running": False,
            "identity_verified": None,
            "confidence": CONFIDENCE_ABSENT,
            "reason": "no pidfile recorded",
        }
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {
            "pid": pid,
            "running": False,
            "identity_verified": None,
            "confidence": CONFIDENCE_ABSENT,
            "reason": "pidfile has no valid pid",
        }
    live = _read_process_identity(pid)
    if live is None:
        return {
            "pid": pid,
            "running": False,
            "identity_verified": None,
            "confidence": CONFIDENCE_STALE,
            "reason": f"no process with pid {pid} is currently running",
        }
    if record.get("format") == "legacy":
        return {
            "pid": pid,
            "running": True,
            "identity_verified": None,
            "confidence": CONFIDENCE_UNVERIFIED,
            "reason": "legacy pidfile recorded no identity to verify against",
        }
    exe_recorded = record.get("exe")
    start_recorded = record.get("start_time")
    exe_live = live.get("exe")
    start_live = live.get("start_time")
    cmdline_recorded = str(record.get("cmdline") or "").lower()
    cmdline_live = str(live.get("cmdline") or "").lower()
    service_cmdline_match = (
        "brains" in cmdline_recorded
        and "serve-all" in cmdline_recorded
        and "brains" in cmdline_live
        and "serve-all" in cmdline_live
    )
    exe_checked = bool(exe_recorded and exe_live)
    start_checked = isinstance(start_recorded, int | float) and isinstance(start_live, int | float)
    if not exe_checked and not start_checked:
        return {
            "pid": pid,
            "running": True,
            "identity_verified": None,
            "confidence": CONFIDENCE_DEGRADED,
            "reason": "process exists but its identity could not be confirmed on this platform",
        }
    exe_match = (_normalize_exe(exe_recorded) == _normalize_exe(exe_live)) if exe_checked else True
    start_match = True
    if start_checked:
        assert isinstance(start_recorded, int | float)
        assert isinstance(start_live, int | float)
        start_match = (
            abs(float(start_recorded) - float(start_live)) <= _START_TIME_TOLERANCE_SECONDS
        )
    if start_checked and exe_match and start_match:
        return {
            "pid": pid,
            "running": True,
            "identity_verified": True,
            "confidence": CONFIDENCE_VERIFIED,
            "reason": "pid and start time match the recorded service; executable matches "
            "when available",
        }
    if exe_match and service_cmdline_match:
        return {
            "pid": pid,
            "running": True,
            "identity_verified": True,
            "confidence": CONFIDENCE_VERIFIED,
            "reason": "pid, executable and Brains serve-all command line match; "
            "platform start time was unavailable or inconsistent",
        }
    if not start_checked and exe_match:
        return {
            "pid": pid,
            "running": True,
            "identity_verified": None,
            "confidence": CONFIDENCE_DEGRADED,
            "reason": "executable matches but process start time could not be verified",
        }
    return {
        "pid": pid,
        "running": True,
        "identity_verified": False,
        "confidence": CONFIDENCE_STALE,
        "reason": "pid is running but its executable/start-time no longer match the recorded "
        "service — this pid was almost certainly reused by an unrelated process",
    }


def cleanup_stale_pidfile(path: Path | None = None) -> dict[str, Any]:
    """Remove ``path`` when it records a stale or absent PID; leave a
    verified/unverifiable-but-running one alone. Returns the driving
    :func:`verify_pid` result plus a ``removed`` bool."""
    target = path or default_pidfile_path()
    record = read_pidfile_record(target)
    result = verify_pid(record)
    if result["confidence"] in (CONFIDENCE_STALE, CONFIDENCE_ABSENT) and target.exists():
        with contextlib.suppress(OSError):
            target.unlink()
        result["removed"] = True
    else:
        result["removed"] = False
    return result


class UnsupportedPlatform(RuntimeError):
    """Raised when no service backend exists for the current OS."""
