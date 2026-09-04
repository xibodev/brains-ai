"""Prove native Claude wakeup recovery without touching an operator installation.

The public mode creates one private temporary root per scenario and launches this
file as a child process with an isolated environment.  Child crash modes use
``os._exit`` deliberately: lock cleanup and Python exception unwinding must not
make recovery look safer than an abrupt harness/process exit really is.

Run this only on disposable Windows or macOS CI runners.  It writes a sanitized
summary containing no paths, settings, credentials, SIDs, or native identifiers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

PHASES = ("prepared", "swapped", "validated", "metadata")
OPERATIONS = ("install", "remove")
CRASH_EXIT_CODE = 86
ORIGINAL_SETTINGS = b'{"synthetic_secret":"not-a-real-secret","hooks":{"Stop":[]}}\r\n'
_CANDIDATE_RE = re.compile(r"^[0-9a-f]{40}$")
_ISOLATED_ENV_KEYS = {
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "TMP",
    "TEMP",
    "BRAINS_STATE_DIR",
    "BRAINS_DB_URL",
    "BRAINS_API_KEY",
    "BRAINS_MCP_BEARER_TOKEN",
    "BRAINS_NATIVE_WAKEUP_PROBE_ROOT",
    "BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _owned(path: Path, root: Path) -> Path:
    if not _inside(path, root):
        raise RuntimeError("probe path escaped its isolated root")
    return path


def _paths(root: Path) -> dict[str, Path]:
    home = _owned(root / "home", root)
    state = _owned(root / "state", root)
    temporary = _owned(root / "tmp", root)
    claude = _owned(home / ".claude", root)
    recovery = _owned(claude / ".brains-wakeup", root)
    return {
        "root": root,
        "home": home,
        "state": state,
        "tmp": temporary,
        "claude": claude,
        "settings": _owned(claude / "settings.json", root),
        "recovery": recovery,
        "lock": _owned(recovery / "settings.lock", root),
        "manifest": _owned(recovery / "manifest.json", root),
        "backup": _owned(recovery / "prior-settings.bin", root),
        "journal": _owned(recovery / "transaction.json", root),
        "capture": _owned(recovery / "displaced-settings.bin", root),
    }


def _isolated_environment(root: Path, candidate: str) -> dict[str, str]:
    paths = _paths(root)
    # Keep only OS/runtime values needed to launch Python and native ACL tools.
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "COMSPEC",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
        }
    }
    inherited.update(
        {
            "HOME": str(paths["home"]),
            "USERPROFILE": str(paths["home"]),
            "XDG_CONFIG_HOME": str(_owned(paths["home"] / ".config", root)),
            "XDG_DATA_HOME": str(_owned(paths["home"] / ".local" / "share", root)),
            "XDG_STATE_HOME": str(_owned(paths["home"] / ".local" / "state", root)),
            "APPDATA": str(_owned(paths["home"] / "AppData" / "Roaming", root)),
            "LOCALAPPDATA": str(_owned(paths["home"] / "AppData" / "Local", root)),
            "TMP": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "BRAINS_STATE_DIR": str(paths["state"]),
            "BRAINS_DB_URL": f"sqlite:///{(paths['state'] / 'brains.db').as_posix()}",
            "BRAINS_API_KEY": "synthetic-native-wakeup-probe-key",
            "BRAINS_MCP_BEARER_TOKEN": "synthetic-native-wakeup-probe-key",
            "BRAINS_NATIVE_WAKEUP_PROBE_ROOT": str(root),
            "BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE": candidate,
        }
    )
    return inherited


def _assert_child_isolation(root: Path, candidate: str) -> dict[str, Path]:
    if os.environ.get("BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE") != candidate:
        raise RuntimeError("probe candidate identity is unavailable")
    paths = _paths(root)
    exact = {
        "HOME": paths["home"],
        "USERPROFILE": paths["home"],
        "XDG_CONFIG_HOME": paths["home"] / ".config",
        "XDG_DATA_HOME": paths["home"] / ".local" / "share",
        "XDG_STATE_HOME": paths["home"] / ".local" / "state",
        "APPDATA": paths["home"] / "AppData" / "Roaming",
        "LOCALAPPDATA": paths["home"] / "AppData" / "Local",
        "TMP": paths["tmp"],
        "TEMP": paths["tmp"],
        "BRAINS_STATE_DIR": paths["state"],
    }
    for key, expected in exact.items():
        raw = os.environ.get(key)
        if raw is None or Path(raw).resolve(strict=False) != expected.resolve(strict=False):
            raise RuntimeError("probe environment is not isolated")
        _owned(Path(raw), root)
    database = os.environ.get("BRAINS_DB_URL", "")
    expected_database = f"sqlite:///{(paths['state'] / 'brains.db').as_posix()}"
    if database != expected_database:
        raise RuntimeError("probe database is not isolated")
    unexpected = {
        key for key in os.environ if key.startswith("BRAINS_") and key not in _ISOLATED_ENV_KEYS
    }
    if unexpected:
        raise RuntimeError("ambient Brains state is detectable by the probe")
    for path in paths.values():
        _owned(path, root)
    return paths


def _wire_context():
    from brains.wire import WireContext

    return WireContext(
        transport="streamable-http",
        url="http://127.0.0.1:1/mcp",
        api_key="synthetic-native-wakeup-probe-key",
    )


def _seed(root: Path, candidate: str, operation: str) -> None:
    paths = _assert_child_isolation(root, candidate)
    for key in ("home", "state", "tmp", "claude"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["settings"].write_bytes(ORIGINAL_SETTINGS)
    if operation == "remove":
        from brains import wire

        report = wire.wire(
            paths["home"],
            _wire_context(),
            tools=["claude-code"],
            rules=False,
            mailbox_wakeups=True,
        )
        if not report.get("ok"):
            raise RuntimeError("native wakeup seed failed")


def _crash(root: Path, candidate: str, operation: str, phase: str) -> NoReturn:
    paths = _assert_child_isolation(root, candidate)
    from brains import wire

    def terminate(name: str) -> None:
        if name == phase:
            os._exit(CRASH_EXIT_CODE)

    wire._TRANSACTION_PHASE_HOOK = terminate
    if operation == "install":
        wire.wire(
            paths["home"],
            _wire_context(),
            tools=["claude-code"],
            rules=False,
            mailbox_wakeups=True,
        )
    else:
        wire.unwire(paths["home"], tools=["claude-code"], rules=False)
    raise RuntimeError("transaction did not reach the requested crash phase")


def _assert_owner_only(path: Path) -> None:
    if os.name == "nt":
        from brains.control.durable_mailbox import (
            _windows_binding_acl_sids,
            _windows_current_user_sid,
        )

        subprocess.run(
            ["icacls", str(path), "/verify"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if _windows_binding_acl_sids(path) != (_windows_current_user_sid(),):
            raise RuntimeError("native recovery ACL is not owner-only")
        return
    expected = 0o700 if path.is_dir() else 0o600
    if stat.S_IMODE(path.stat().st_mode) != expected:
        raise RuntimeError("native recovery mode is not owner-only")


def _installed_settings(path: Path) -> bool:
    from brains.wire import _claude_wakeup_entry

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["hooks"]["Stop"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(entries, list) and _claude_wakeup_entry() in entries


def _inspect_interrupted(
    root: Path,
    candidate: str,
    operation: str,
    phase: str,
) -> None:
    paths = _assert_child_isolation(root, candidate)
    expected_present = {"recovery", "lock", "journal", "settings"}
    if phase != "prepared":
        expected_present.add("capture")
    if operation == "remove" or phase == "metadata":
        expected_present.add("backup")
    if operation == "remove" and phase != "metadata":
        expected_present.add("manifest")
    if operation == "install" and phase == "metadata":
        expected_present.add("manifest")
    for key in expected_present:
        if not paths[key].exists():
            raise RuntimeError("native recovery artifact is missing")
    for key in ("lock", "journal", "capture", "manifest", "backup"):
        if paths[key].exists():
            _assert_owner_only(paths[key])
    _assert_owner_only(paths["recovery"])
    if operation == "remove" or phase != "prepared":
        _assert_owner_only(paths["settings"])

    target_is_installed = _installed_settings(paths["settings"])
    expected_installed = operation == "remove" and phase == "prepared"
    if operation == "install" and phase != "prepared":
        expected_installed = True
    if target_is_installed != expected_installed:
        raise RuntimeError("native atomic exchange state is inconsistent")

    if paths["backup"].exists():
        from brains import wire

        manifest = wire._read_wakeup_manifest(paths["manifest"])
        protection = (
            manifest.get("prior_protection")
            if manifest is not None
            else ("windows-dpapi" if os.name == "nt" else "posix-owner")
        )
        if wire._read_prior_backup(paths["backup"], str(protection)) != ORIGINAL_SETTINGS:
            raise RuntimeError("native recovery backup does not restore exact bytes")


def _recover(root: Path, candidate: str, operation: str) -> None:
    paths = _assert_child_isolation(root, candidate)
    from brains import wire

    if operation == "install":
        report = wire.wire(
            paths["home"],
            _wire_context(),
            tools=["claude-code"],
            rules=False,
            mailbox_wakeups=True,
        )
        if not report.get("ok"):
            raise RuntimeError("native install recovery failed")
        removed = wire.unwire(paths["home"], tools=["claude-code"], rules=False)
        action = removed["tools"][0]["mailbox_wakeup"].get("action")
        if action != "remove":
            raise RuntimeError("native install recovery rollback failed")
    else:
        removed = wire.unwire(paths["home"], tools=["claude-code"], rules=False)
        action = removed["tools"][0]["mailbox_wakeup"].get("action")
        if action != "remove":
            raise RuntimeError("native removal recovery failed")
    if paths["settings"].read_bytes() != ORIGINAL_SETTINGS:
        raise RuntimeError("native recovery did not restore exact settings bytes")
    for key in ("lock", "manifest", "backup", "journal", "capture"):
        if paths[key].exists():
            raise RuntimeError("native recovery left transaction state behind")


def _child(arguments: argparse.Namespace) -> int:
    if not arguments.root or not arguments.operation:
        raise RuntimeError("incomplete native probe child invocation")
    root = Path(arguments.root).resolve(strict=True)
    operation = arguments.operation
    if operation not in OPERATIONS:
        raise RuntimeError("unsupported native probe operation")
    if arguments.child_action == "seed":
        _seed(root, arguments.candidate, operation)
    elif arguments.child_action == "crash":
        if arguments.phase not in PHASES:
            raise RuntimeError("unsupported native probe phase")
        _crash(root, arguments.candidate, operation, arguments.phase)
    elif arguments.child_action == "inspect":
        if arguments.phase not in PHASES:
            raise RuntimeError("unsupported native probe phase")
        _inspect_interrupted(root, arguments.candidate, operation, arguments.phase)
    elif arguments.child_action == "recover":
        _recover(root, arguments.candidate, operation)
    else:
        raise RuntimeError("unsupported native probe child action")
    return 0


def _run_child(
    script: Path,
    root: Path,
    candidate: str,
    action: str,
    operation: str,
    phase: str | None = None,
    *,
    expected: int = 0,
) -> None:
    command = [
        sys.executable,
        str(script),
        "--candidate",
        candidate,
        "--child-action",
        action,
        "--root",
        str(root),
        "--operation",
        operation,
    ]
    if phase is not None:
        command.extend(["--phase", phase])
    completed = subprocess.run(
        command,
        env=_isolated_environment(root, candidate),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != expected:
        # Never relay child output: exception text can contain environment paths.
        raise RuntimeError("isolated native wakeup probe child failed")


def _write_public_result(output: str | None, result: dict[str, object]) -> None:
    rendered = json.dumps(result, sort_keys=True) + "\n"
    if output:
        target = Path(output)
        working_root = Path.cwd().resolve(strict=True)
        try:
            target.parent.resolve(strict=True).relative_to(working_root)
        except (OSError, ValueError) as exc:
            raise RuntimeError("probe report path escaped the working directory") from exc
        if target.exists() or target.is_symlink():
            raise RuntimeError("probe report path already exists")
        target.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def _public(arguments: argparse.Namespace) -> int:
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError("native wakeup recovery proof requires Windows or macOS")
    if not _CANDIDATE_RE.fullmatch(arguments.candidate):
        raise RuntimeError("exact candidate identity is required")
    script = Path(__file__).resolve(strict=True)
    scenarios = 0
    for operation in OPERATIONS:
        for phase in PHASES:
            with tempfile.TemporaryDirectory(prefix="brains-native-wakeup-") as raw:
                root = Path(raw).resolve(strict=True)
                paths = _paths(root)
                for key in ("home", "state", "tmp"):
                    paths[key].mkdir(parents=True, exist_ok=True)
                _run_child(script, root, arguments.candidate, "seed", operation)
                _run_child(
                    script,
                    root,
                    arguments.candidate,
                    "crash",
                    operation,
                    phase,
                    expected=CRASH_EXIT_CODE,
                )
                _run_child(script, root, arguments.candidate, "inspect", operation, phase)
                _run_child(script, root, arguments.candidate, "recover", operation)
                scenarios += 1
    result = {
        "ok": True,
        "candidate": arguments.candidate,
        "platform": "windows" if os.name == "nt" else "macos",
        "atomic_primitive": "ReplaceFileW" if os.name == "nt" else "renamex_np(RENAME_SWAP)",
        "recovery_boundary": "dpapi-current-user-dacl" if os.name == "nt" else "posix-owner-only",
        "scenarios": scenarios,
    }
    _write_public_result(arguments.output, result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output")
    parser.add_argument("--child-action", choices=("seed", "crash", "inspect", "recover"))
    parser.add_argument("--root")
    parser.add_argument("--operation", choices=OPERATIONS)
    parser.add_argument("--phase", choices=PHASES)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.child_action:
            return _child(arguments)
        return _public(arguments)
    except Exception:
        # A child is always captured by its parent and must not leak a traceback
        # containing temporary paths.  The public runner emits one bounded report.
        if arguments.child_action:
            return 1
        candidate = (
            arguments.candidate if _CANDIDATE_RE.fullmatch(arguments.candidate) else "unverified"
        )
        failure = {
            "ok": False,
            "candidate": candidate,
            "platform": (
                "windows"
                if os.name == "nt"
                else ("macos" if sys.platform == "darwin" else "unsupported")
            ),
            "reason": "native-wakeup-recovery-proof-failed",
        }
        try:
            _write_public_result(arguments.output, failure)
        except Exception:
            sys.stdout.write(json.dumps(failure, sort_keys=True) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
