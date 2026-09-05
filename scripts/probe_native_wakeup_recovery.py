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
import base64
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import NoReturn

PHASES = ("prepared", "swapped", "validated", "metadata")
OPERATIONS = ("install", "remove")
CRASH_EXIT_CODE = 86
ORIGINAL_SETTINGS = b'{"synthetic_secret":"not-a-real-secret","hooks":{"Stop":[]}}\r\n'
_CANDIDATE_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_PROVENANCE_SCHEMA = "brains-native-wakeup-package-provenance/v1"
RUNTIME_PROVENANCE_SCHEMA = "brains-native-wakeup-runtime-provenance/v1"
# Mirrors brains.control.durable_mailbox.WINDOWS_OS_PRINCIPAL_SIDS. This probe
# runs on the bootstrap interpreter, which has no Brains package to import from.
_WINDOWS_OS_PRINCIPAL_SIDS = frozenset(
    {
        "S-1-3-4",  # OWNER RIGHTS
        "S-1-5-18",  # LOCAL SYSTEM
        "S-1-5-32-544",  # BUILTIN\\Administrators
    }
)
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
    "BRAINS_NATIVE_WAKEUP_PROBE_PROVENANCE_DIGEST",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_path(raw: str) -> Path:
    supplied = Path(raw)
    if not supplied.is_absolute():
        raise RuntimeError("candidate source tool must be an absolute path")
    git = supplied.resolve(strict=True)
    if git.name.casefold() not in {"git", "git.exe"} or not git.is_file():
        raise RuntimeError("candidate source tool is invalid")
    return git


def _git(root: Path, git: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [str(git), "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("candidate source identity is unavailable")
    return completed.stdout.strip()


def _source_identity(root: Path, candidate: str, git_executable: str) -> dict[str, str]:
    root = root.resolve(strict=True)
    git = _git_path(git_executable)
    head = _git(root, git, "rev-parse", "HEAD").lower()
    tree = _git(root, git, "rev-parse", "HEAD^{tree}").lower()
    if candidate.lower() != head or not _CANDIDATE_RE.fullmatch(head):
        raise RuntimeError("candidate does not match checked-out source")
    if not _CANDIDATE_RE.fullmatch(tree):
        raise RuntimeError("candidate source tree identity is unavailable")
    for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
        completed = subprocess.run(
            [str(git), "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError("candidate checkout has tracked source drift")
    return {"candidate": head, "source_tree": tree}


def _wheel_attestation(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if any(len(names) != 1 for names in (record_names, metadata_names, wheel_names)):
            raise RuntimeError("wheel distribution identity is ambiguous")
        rows = sorted(csv.reader(archive.read(record_names[0]).decode("utf-8").splitlines()))
        for relative, digest, _size in rows:
            if not digest:
                continue
            algorithm, encoded = digest.split("=", 1)
            if algorithm != "sha256":
                raise RuntimeError("wheel RECORD uses an unsupported digest")
            if hashlib.sha256(archive.read(relative)).digest() != _decode_record_digest(encoded):
                raise RuntimeError("wheel RECORD verification failed")
        metadata = archive.read(metadata_names[0])
        wheel_metadata = archive.read(wheel_names[0])
    return {
        "wheel_record_sha256": _canonical_digest(rows),
        "wheel_archive_metadata_sha256": hashlib.sha256(metadata).hexdigest(),
        "wheel_archive_wheel_sha256": hashlib.sha256(wheel_metadata).hexdigest(),
    }


def _write_package_manifest(
    source_root: Path,
    candidate: str,
    wheel: Path,
    output: Path,
    git_executable: str,
) -> dict[str, str]:
    git = _git_path(git_executable)
    identity = _source_identity(source_root, candidate, str(git))
    wheel = wheel.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise RuntimeError("package provenance path already exists")
    manifest = {
        "schema": PACKAGE_PROVENANCE_SCHEMA,
        **identity,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel),
        **_wheel_attestation(wheel),
        "builder_git_sha256": _sha256(git),
    }
    output.parent.resolve(strict=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True)
        handle.write("\n")
    return manifest


def _read_package_manifest(
    manifest_path: Path,
    source_root: Path,
    candidate: str,
    wheel: Path,
    git_executable: str,
) -> dict[str, str]:
    git = _git_path(git_executable)
    identity = _source_identity(source_root, candidate, str(git))
    try:
        raw = json.loads(manifest_path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("package provenance is unavailable") from exc
    expected = {
        "schema": PACKAGE_PROVENANCE_SCHEMA,
        **identity,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel),
        **_wheel_attestation(wheel),
    }
    expected_keys = {*expected, "builder_git_sha256"}
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or {key: raw.get(key) for key in expected} != expected
        or not _DIGEST_RE.fullmatch(str(raw.get("builder_git_sha256", "")))
    ):
        raise RuntimeError("package provenance does not match candidate wheel")
    return {key: str(value) for key, value in raw.items()}


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


def _make_private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        expected = 0o700 if path.is_dir() else 0o600
        if stat.S_IMODE(path.stat().st_mode) != expected:
            raise RuntimeError("private probe path mode is not owner-only")
        return
    system_root = Path(os.environ["SYSTEMROOT"]).resolve(strict=True)
    powershell = (system_root / "System32/WindowsPowerShell/v1.0/powershell.exe").resolve(
        strict=True
    )
    icacls = (system_root / "System32/icacls.exe").resolve(strict=True)
    whoami = (system_root / "System32/whoami.exe").resolve(strict=True)
    identity = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    rows = list(csv.reader(identity.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2:
        raise RuntimeError("current Windows SID is unavailable")
    sid = rows[0][1]
    grant = f"*{sid}:(OI)(CI)(F)" if path.is_dir() else f"*{sid}:(F)"
    subprocess.run(
        [str(icacls), str(path), "/inheritance:r", "/grant:r", grant],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        [str(icacls), str(path), "/verify"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    environment = {**os.environ, "BRAINS_NATIVE_WAKEUP_ACL_PATH": str(path)}
    # Get-Acl lives in a Windows PowerShell 5.1 module found through
    # PSModulePath. This probe is launched from PowerShell 7, which exports that
    # edition's path, so 5.1 cannot load its security module and reports nothing.
    environment["PSModulePath"] = str(system_root / "System32/WindowsPowerShell/v1.0/Modules")
    acl = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; "
            "$acl = Get-Acl -LiteralPath $env:BRAINS_NATIVE_WAKEUP_ACL_PATH; "
            "$acl.Access | ForEach-Object { "
            "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
            "}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    ).stdout
    acl_sids = tuple(sorted({line.strip() for line in acl.splitlines() if line.strip()}))
    # An administrator-owned path keeps LOCAL SYSTEM, BUILTIN\Administrators and
    # OWNER RIGHTS, all of which reach the file through OS semantics whatever the
    # DACL says. Require the owner and reject any other principal. An empty
    # result means the ACL was unreadable, not that nobody is granted access.
    unexpected = tuple(
        value for value in acl_sids if value != sid and value not in _WINDOWS_OS_PRINCIPAL_SIDS
    )
    if not acl_sids or sid not in acl_sids or unexpected:
        raise RuntimeError("private probe path ACL is not owner-only")


def _new_private_invocation(invocation: Path) -> Path:
    if not invocation.is_absolute():
        raise RuntimeError("evidence invocation path must be absolute")
    supplied_parent = invocation.parent
    if supplied_parent.is_symlink():
        raise RuntimeError("evidence invocation parent is not a real directory")
    parent = supplied_parent.resolve(strict=True)
    if not parent.is_dir():
        raise RuntimeError("evidence invocation parent is not a real directory")
    invocation = parent / invocation.name
    if invocation.exists() or invocation.is_symlink():
        raise RuntimeError("evidence invocation path already exists")
    invocation.mkdir()
    try:
        if any(invocation.iterdir()):
            raise RuntimeError("new evidence invocation directory is not empty")
        _make_private(invocation)
    except Exception:
        shutil.rmtree(invocation, ignore_errors=True)
        raise
    return invocation


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_launcher(root: Path) -> Path:
    return root / ("Scripts/brains-ai.exe" if os.name == "nt" else "bin/brains-ai")


def _controlled_path(venv_root: Path | None = None) -> str:
    entries: list[str] = []
    if venv_root is not None:
        entries.append(str(_venv_python(venv_root).parent.resolve(strict=True)))
    if os.name == "nt":
        system_root = Path(os.environ["SYSTEMROOT"]).resolve(strict=True)
        entries.extend(
            (
                str((system_root / "System32").resolve(strict=True)),
                str((system_root / "System32/WindowsPowerShell/v1.0").resolve(strict=True)),
            )
        )
    else:
        entries.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return os.pathsep.join(entries)


def _worker_environment(invocation: Path, git: Path, venv_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
    }
    environment.update(
        {
            "HOME": str(invocation),
            "USERPROFILE": str(invocation),
            "TMP": str(invocation),
            "TEMP": str(invocation),
            "PATH": _controlled_path(venv_root),
            "PIP_NO_CACHE_DIR": "1",
            "BRAINS_NATIVE_WAKEUP_PROBE_GIT_SHA256": _sha256(git),
        }
    )
    if os.name == "nt":
        environment["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return environment


def _decode_record_digest(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _runtime_provenance(package: dict[str, str], wheel: Path) -> dict[str, str]:
    distribution = importlib.metadata.distribution("brains-ai")
    prefix = Path(sys.prefix).resolve(strict=True)
    if Path(sys.base_prefix).resolve(strict=True) == prefix:
        raise RuntimeError("native probe is not running from a dedicated virtual environment")
    expected_python = _venv_python(prefix).resolve(strict=True)
    if Path(sys.executable).resolve(strict=True) != expected_python:
        raise RuntimeError("native probe interpreter is not the exact virtual-environment Python")

    record_text = distribution.read_text("RECORD")
    metadata_text = distribution.read_text("METADATA")
    wheel_text = distribution.read_text("WHEEL")
    direct_url_text = distribution.read_text("direct_url.json")
    if None in {record_text, metadata_text, wheel_text, direct_url_text}:
        raise RuntimeError("installed distribution provenance is incomplete")
    assert record_text is not None
    assert metadata_text is not None
    assert wheel_text is not None
    assert direct_url_text is not None
    if (
        hashlib.sha256(metadata_text.encode("utf-8")).hexdigest()
        != package["wheel_archive_metadata_sha256"]
    ):
        raise RuntimeError("installed METADATA differs from the candidate wheel")
    if (
        hashlib.sha256(wheel_text.encode("utf-8")).hexdigest()
        != package["wheel_archive_wheel_sha256"]
    ):
        raise RuntimeError("installed WHEEL differs from the candidate wheel")
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("installed direct_url provenance is invalid") from exc
    archive_info = direct_url.get("archive_info", {}) if isinstance(direct_url, dict) else {}
    hashes = archive_info.get("hashes", {}) if isinstance(archive_info, dict) else {}
    if (
        not isinstance(direct_url, dict)
        or direct_url.get("url") != wheel.resolve(strict=True).as_uri()
        or not isinstance(archive_info, dict)
        or archive_info.get("hash") != f"sha256={package['wheel_sha256']}"
        or not isinstance(hashes, dict)
        or hashes.get("sha256") != package["wheel_sha256"]
    ):
        raise RuntimeError("installed direct_url does not identify the exact candidate wheel")
    rows = sorted(csv.reader(record_text.splitlines()))
    distribution_root = Path(distribution.locate_file("")).resolve(strict=True)
    try:
        distribution_root.relative_to(prefix)
    except ValueError as exc:
        raise RuntimeError("installed distribution escaped its virtual environment") from exc
    for relative, digest, _size in rows:
        if not digest:
            continue
        algorithm, encoded = digest.split("=", 1)
        if algorithm != "sha256":
            raise RuntimeError("installed RECORD uses an unsupported digest")
        installed = Path(distribution.locate_file(relative)).resolve(strict=True)
        try:
            installed.relative_to(prefix)
        except ValueError as exc:
            raise RuntimeError("installed RECORD path escaped its virtual environment") from exc
        if hashlib.sha256(installed.read_bytes()).digest() != _decode_record_digest(encoded):
            raise RuntimeError("installed distribution RECORD verification failed")
    with zipfile.ZipFile(wheel) as archive:
        wheel_record_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/RECORD")
        )
        wheel_rows = sorted(
            csv.reader(archive.read(wheel_record_name).decode("utf-8").splitlines())
        )
    for relative, digest, _size in wheel_rows:
        if not digest:
            continue
        algorithm, encoded = digest.split("=", 1)
        if algorithm != "sha256":
            raise RuntimeError("candidate wheel RECORD uses an unsupported digest")
        installed = Path(distribution.locate_file(relative)).resolve(strict=True)
        try:
            installed.relative_to(prefix)
        except ValueError as exc:
            raise RuntimeError("candidate wheel file escaped its virtual environment") from exc
        if hashlib.sha256(installed.read_bytes()).digest() != _decode_record_digest(encoded):
            raise RuntimeError("installed package file differs from the candidate wheel RECORD")

    import brains

    module = Path(brains.__file__).resolve(strict=True)
    launcher = _venv_launcher(prefix).resolve(strict=True)
    for installed in (module, launcher, expected_python):
        try:
            installed.relative_to(prefix)
        except ValueError as exc:
            raise RuntimeError(
                "installed executable or module escaped its virtual environment"
            ) from exc
    safe = {
        **package,
        "schema": RUNTIME_PROVENANCE_SCHEMA,
        "runner_git_sha256": os.environ["BRAINS_NATIVE_WAKEUP_PROBE_GIT_SHA256"],
        "installed_record_sha256": _canonical_digest(rows),
        "installed_metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
        "installed_wheel_sha256": hashlib.sha256(wheel_text.encode("utf-8")).hexdigest(),
        "direct_url_sha256": hashlib.sha256(direct_url_text.encode("utf-8")).hexdigest(),
        "module_sha256": _sha256(module),
        "launcher_sha256": _sha256(launcher),
        "interpreter_sha256": _sha256(expected_python),
        "installed_version": distribution.version,
    }
    if safe["wheel_sha256"] != _sha256(wheel):
        raise RuntimeError("runtime wheel identity changed after attestation")
    return safe


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


def _isolated_environment(root: Path, candidate: str, provenance_digest: str) -> dict[str, str]:
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
            "LANG",
            "LC_ALL",
        }
    }
    if os.name == "nt":
        inherited["PATH"] = _controlled_path()
        inherited["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    else:
        inherited["PATH"] = _controlled_path()
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
            "BRAINS_NATIVE_WAKEUP_PROBE_PROVENANCE_DIGEST": provenance_digest,
        }
    )
    return inherited


def _assert_child_isolation(root: Path, candidate: str, provenance_digest: str) -> dict[str, Path]:
    if os.environ.get("BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE") != candidate:
        raise RuntimeError("probe candidate identity is unavailable")
    if (
        not _DIGEST_RE.fullmatch(provenance_digest)
        or os.environ.get("BRAINS_NATIVE_WAKEUP_PROBE_PROVENANCE_DIGEST") != provenance_digest
    ):
        raise RuntimeError("probe runtime provenance is unavailable")
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


def _claude_adapter(wire, home: Path):
    selected = wire._select_adapters(["claude-code"], home, True)
    if len(selected) != 1 or selected[0].name != "claude-code":
        raise RuntimeError("Claude wakeup adapter is unavailable")
    return selected[0]


def _home_snapshot(home: Path) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {".": {"kind": "directory"}}
    if os.name != "nt":
        snapshot["."]["mode"] = stat.S_IMODE(home.stat().st_mode)
    for path in sorted(home.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("synthetic Claude home contains a symlink")
        relative = path.relative_to(home).as_posix()
        if path.is_dir():
            row: dict[str, object] = {"kind": "directory"}
        elif path.is_file():
            row = {"kind": "file", "size": path.stat().st_size, "sha256": _sha256(path)}
        else:
            raise RuntimeError("synthetic Claude home contains an unsupported entry")
        if os.name != "nt":
            row["mode"] = stat.S_IMODE(path.stat().st_mode)
        snapshot[relative] = row
    return snapshot


def _write_baseline(root: Path, snapshot: dict[str, dict[str, object]]) -> None:
    baseline = _owned(root / "baseline-home.json", root)
    if baseline.exists() or baseline.is_symlink():
        raise RuntimeError("synthetic home baseline already exists")
    baseline.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")


def _read_baseline(root: Path) -> dict[str, dict[str, object]]:
    baseline = _owned(root / "baseline-home.json", root)
    try:
        value = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("synthetic home baseline is unavailable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("synthetic home baseline is invalid")
    return value


def _seed(root: Path, candidate: str, provenance_digest: str, operation: str) -> None:
    paths = _assert_child_isolation(root, candidate, provenance_digest)
    for key in ("home", "state", "tmp", "claude"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["settings"].write_bytes(ORIGINAL_SETTINGS)
    (paths["home"] / ".claude.json").write_text(
        '{"synthetic_client_marker":true}\n', encoding="utf-8"
    )
    sentinel = paths["claude"] / "profiles" / "synthetic.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"synthetic-claude-client-state\x00\xff")
    if os.name != "nt":
        for directory in (
            paths["home"],
            paths["claude"],
            sentinel.parent,
        ):
            directory.chmod(0o700)
        for file_path in (paths["settings"], paths["home"] / ".claude.json", sentinel):
            file_path.chmod(0o600)
    from brains import wire

    normal = wire.wire(
        paths["home"],
        _wire_context(),
        tools=["claude-code"],
        rules=False,
        mailbox_wakeups=False,
    )
    if not normal.get("ok"):
        raise RuntimeError("normal Claude wire setup failed")
    _write_baseline(root, _home_snapshot(paths["home"]))
    if operation == "remove":
        wakeup = wire._wire_wakeup(_claude_adapter(wire, paths["home"]), paths["home"], False)
        if wakeup.get("action") not in {"create", "update", "recovered", "unchanged"}:
            raise RuntimeError("native wakeup removal baseline failed")


def _crash(
    root: Path, candidate: str, provenance_digest: str, operation: str, phase: str
) -> NoReturn:
    paths = _assert_child_isolation(root, candidate, provenance_digest)
    from brains import wire

    def terminate(name: str) -> None:
        if name == phase:
            os._exit(CRASH_EXIT_CODE)

    wire._TRANSACTION_PHASE_HOOK = terminate
    adapter = _claude_adapter(wire, paths["home"])
    if operation == "install":
        wire._wire_wakeup(adapter, paths["home"], False)
    else:
        wire._unwire_wakeup(adapter, paths["home"], False)
    raise RuntimeError("transaction did not reach the requested crash phase")


def _assert_owner_only(path: Path) -> None:
    if os.name == "nt":
        from brains.control.durable_mailbox import (
            _windows_binding_acl_sids,
            _windows_current_user_sid,
        )

        subprocess.run(
            [
                str(
                    (
                        Path(os.environ["SYSTEMROOT"]).resolve(strict=True) / "System32/icacls.exe"
                    ).resolve(strict=True)
                ),
                str(path),
                "/verify",
            ],
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
    provenance_digest: str,
    operation: str,
    phase: str,
) -> None:
    paths = _assert_child_isolation(root, candidate, provenance_digest)
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


def _recover(root: Path, candidate: str, provenance_digest: str, operation: str) -> None:
    paths = _assert_child_isolation(root, candidate, provenance_digest)
    from brains import wire

    adapter = _claude_adapter(wire, paths["home"])
    if operation == "install":
        completed = wire._wire_wakeup(adapter, paths["home"], False)
        if completed.get("action") not in {"create", "update", "recovered", "unchanged"}:
            raise RuntimeError("native install recovery completion failed")
        rollback = wire._unwire_wakeup(adapter, paths["home"], False)
        if rollback.get("action") != "remove":
            raise RuntimeError("native install rollback failed")
    else:
        rollback = wire._unwire_wakeup(adapter, paths["home"], False)
        if rollback.get("action") != "remove":
            raise RuntimeError("native removal rollback failed")
    for key in ("lock", "manifest", "backup", "journal", "capture"):
        if paths[key].exists():
            raise RuntimeError("native recovery left transaction state behind")
    if paths["recovery"].exists():
        raise RuntimeError("native recovery directory survived successful restoration")
    if _home_snapshot(paths["home"]) != _read_baseline(root):
        raise RuntimeError("native recovery changed the synthetic Claude client home")


def _child(arguments: argparse.Namespace) -> int:
    if not arguments.root or not arguments.operation:
        raise RuntimeError("incomplete native probe child invocation")
    root = Path(arguments.root).resolve(strict=True)
    operation = arguments.operation
    if operation not in OPERATIONS:
        raise RuntimeError("unsupported native probe operation")
    provenance_digest = arguments.provenance_digest
    if not provenance_digest:
        raise RuntimeError("native probe child provenance is absent")
    if arguments.child_action == "seed":
        _seed(root, arguments.candidate, provenance_digest, operation)
    elif arguments.child_action == "crash":
        if arguments.phase not in PHASES:
            raise RuntimeError("unsupported native probe phase")
        _crash(root, arguments.candidate, provenance_digest, operation, arguments.phase)
    elif arguments.child_action == "inspect":
        if arguments.phase not in PHASES:
            raise RuntimeError("unsupported native probe phase")
        _inspect_interrupted(
            root,
            arguments.candidate,
            provenance_digest,
            operation,
            arguments.phase,
        )
    elif arguments.child_action == "recover":
        _recover(root, arguments.candidate, provenance_digest, operation)
    else:
        raise RuntimeError("unsupported native probe child action")
    return 0


def _run_child(
    script: Path,
    root: Path,
    candidate: str,
    provenance_digest: str,
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
        "--provenance-digest",
        provenance_digest,
    ]
    if phase is not None:
        command.extend(["--phase", phase])
    # action, operation and phase are closed vocabularies checked by the parser,
    # so naming them identifies a stuck step without quoting any host value.
    step = f"{action}/{operation}" + (f"/{phase}" if phase else "")
    try:
        completed = subprocess.run(
            command,
            env=_isolated_environment(root, candidate, provenance_digest),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"isolated native wakeup probe child timed out at {step}") from exc
    if completed.returncode != expected:
        # Never relay child output: exception text can contain environment paths.
        raise RuntimeError(f"isolated native wakeup probe child failed at {step}")


def _write_public_result(output: Path, result: dict[str, object]) -> None:
    rendered = json.dumps(result, sort_keys=True) + "\n"
    if output.exists() or output.is_symlink():
        raise RuntimeError("probe report path already exists")
    with output.open("x", encoding="utf-8") as handle:
        handle.write(rendered)
    _make_private(output)
    sys.stdout.write(rendered)


def _validate_result(
    result: object,
    provenance: dict[str, str],
    package: dict[str, str],
) -> dict[str, object]:
    if not isinstance(result, dict):
        raise RuntimeError("native wakeup evidence is not an object")
    expected_provenance_keys = {
        "schema",
        "candidate",
        "source_tree",
        "wheel_filename",
        "wheel_sha256",
        "wheel_record_sha256",
        "wheel_archive_metadata_sha256",
        "wheel_archive_wheel_sha256",
        "builder_git_sha256",
        "runner_git_sha256",
        "installed_record_sha256",
        "installed_metadata_sha256",
        "installed_wheel_sha256",
        "direct_url_sha256",
        "module_sha256",
        "launcher_sha256",
        "interpreter_sha256",
        "installed_version",
    }
    if set(provenance) != expected_provenance_keys:
        raise RuntimeError("native wakeup provenance schema is invalid")
    if provenance["schema"] != RUNTIME_PROVENANCE_SCHEMA:
        raise RuntimeError("native wakeup provenance version is invalid")
    for key in ("candidate", "source_tree"):
        if not _CANDIDATE_RE.fullmatch(provenance[key]):
            raise RuntimeError("native wakeup source identity is invalid")
    for key in expected_provenance_keys:
        if key.endswith("_sha256") and not _DIGEST_RE.fullmatch(provenance[key]):
            raise RuntimeError("native wakeup provenance digest is invalid")
    for key in (
        "candidate",
        "source_tree",
        "wheel_filename",
        "wheel_sha256",
        "wheel_record_sha256",
        "wheel_archive_metadata_sha256",
        "wheel_archive_wheel_sha256",
        "builder_git_sha256",
    ):
        if provenance[key] != package[key]:
            raise RuntimeError("runtime provenance differs from package attestation")
    if Path(provenance["wheel_filename"]).name != provenance["wheel_filename"]:
        raise RuntimeError("native wakeup wheel identity is invalid")
    digest = _canonical_digest(provenance)
    expected = {
        "ok": True,
        "candidate": provenance["candidate"],
        "platform": "windows" if os.name == "nt" else "macos",
        "atomic_primitive": "ReplaceFileW" if os.name == "nt" else "renamex_np(RENAME_SWAP)",
        "recovery_boundary": ("dpapi-current-user-dacl" if os.name == "nt" else "posix-owner-only"),
        "scenarios": len(PHASES) * len(OPERATIONS),
        "home_snapshot_restored": True,
        "recovery_directories_removed": True,
        "child_processes_reaped": True,
        "listeners_started": False,
        "provenance": provenance,
        "provenance_digest": digest,
    }
    if result != expected:
        raise RuntimeError("native wakeup evidence schema or provenance is invalid")
    return result


def _verify_existing_result(arguments: argparse.Namespace) -> int:
    if not all(
        (
            arguments.git_executable,
            arguments.wheel,
            arguments.package_manifest,
            arguments.result,
        )
    ):
        raise RuntimeError("native evidence verification inputs are incomplete")
    source_root = Path(__file__).resolve(strict=True).parents[1]
    git = _git_path(arguments.git_executable)
    wheel = Path(arguments.wheel).resolve(strict=True)
    package = _read_package_manifest(
        Path(arguments.package_manifest),
        source_root,
        arguments.candidate,
        wheel,
        str(git),
    )
    result_path = Path(arguments.result).resolve(strict=True)
    if result_path.name != "native-wakeup-recovery.json" or result_path.parent.is_symlink():
        raise RuntimeError("native evidence path is invalid")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    provenance = result.get("provenance") if isinstance(result, dict) else None
    if not isinstance(provenance, dict):
        raise RuntimeError("native wakeup provenance is absent")
    if provenance.get("runner_git_sha256") != _sha256(git):
        raise RuntimeError("native evidence Git provenance does not match the runner")
    _validate_result(result, provenance, package)
    sys.stdout.write(
        json.dumps({"ok": True, "provenance_digest": result["provenance_digest"]}, sort_keys=True)
        + "\n"
    )
    return 0


def _worker(arguments: argparse.Namespace) -> int:
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError("native wakeup recovery proof requires Windows or macOS")
    if not _CANDIDATE_RE.fullmatch(arguments.candidate):
        raise RuntimeError("exact candidate identity is required")
    required = (
        arguments.source_root,
        arguments.git_executable,
        arguments.wheel,
        arguments.package_manifest,
        arguments.invocation_root,
        arguments.output,
    )
    if not all(required):
        raise RuntimeError("native worker attestation inputs are incomplete")
    script = Path(__file__).resolve(strict=True)
    source_root = Path(arguments.source_root).resolve(strict=True)
    git = _git_path(arguments.git_executable)
    if os.environ.get("BRAINS_NATIVE_WAKEUP_PROBE_GIT_SHA256") != _sha256(git):
        raise RuntimeError("native worker Git provenance is unavailable")
    wheel = Path(arguments.wheel).resolve(strict=True)
    invocation = Path(arguments.invocation_root).resolve(strict=True)
    output = _owned(Path(arguments.output), invocation)
    package = _read_package_manifest(
        Path(arguments.package_manifest),
        source_root,
        arguments.candidate,
        wheel,
        str(git),
    )
    provenance = _runtime_provenance(package, wheel)
    provenance_digest = _canonical_digest(provenance)
    scratch = _owned(invocation / "scenarios", invocation)
    if scratch.exists() or scratch.is_symlink():
        raise RuntimeError("native scenario root already exists")
    scratch.mkdir()
    _make_private(scratch)
    scenarios = 0
    try:
        for operation in OPERATIONS:
            for phase in PHASES:
                with tempfile.TemporaryDirectory(prefix="scenario-", dir=scratch) as raw:
                    root = Path(raw).resolve(strict=True)
                    _make_private(root)
                    paths = _paths(root)
                    for key in ("home", "state", "tmp"):
                        paths[key].mkdir(parents=True, exist_ok=True)
                    _run_child(
                        script,
                        root,
                        arguments.candidate,
                        provenance_digest,
                        "seed",
                        operation,
                    )
                    _run_child(
                        script,
                        root,
                        arguments.candidate,
                        provenance_digest,
                        "crash",
                        operation,
                        phase,
                        expected=CRASH_EXIT_CODE,
                    )
                    _run_child(
                        script,
                        root,
                        arguments.candidate,
                        provenance_digest,
                        "inspect",
                        operation,
                        phase,
                    )
                    _run_child(
                        script,
                        root,
                        arguments.candidate,
                        provenance_digest,
                        "recover",
                        operation,
                    )
                    scenarios += 1
    finally:
        shutil.rmtree(scratch, ignore_errors=False)
    if scratch.exists():
        raise RuntimeError("native scenario evidence survived cleanup")
    result = {
        "ok": True,
        "candidate": package["candidate"],
        "platform": "windows" if os.name == "nt" else "macos",
        "atomic_primitive": "ReplaceFileW" if os.name == "nt" else "renamex_np(RENAME_SWAP)",
        "recovery_boundary": "dpapi-current-user-dacl" if os.name == "nt" else "posix-owner-only",
        "scenarios": scenarios,
        "home_snapshot_restored": True,
        "recovery_directories_removed": True,
        "child_processes_reaped": True,
        "listeners_started": False,
        "provenance": provenance,
        "provenance_digest": provenance_digest,
    }
    _validate_result(result, provenance, package)
    _write_public_result(output, result)
    return 0


def _worker_failure_detail(stdout: str) -> str:
    """Return the worker's own bounded reason for failing.

    The worker emits the same curated report this runner does, so forwarding it
    names the rejected contract without quoting a path or host value.
    """
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            reported = payload.get("detail") or payload.get("reason")
            if isinstance(reported, str) and reported:
                return reported[:200]
        break
    return "no bounded worker report"


def _bootstrap(arguments: argparse.Namespace) -> int:
    if not all(
        (
            arguments.wheel,
            arguments.package_manifest,
            arguments.invocation_path,
            arguments.git_executable,
        )
    ):
        raise RuntimeError("native probe bootstrap inputs are incomplete")
    source_root = Path(__file__).resolve(strict=True).parents[1]
    git = _git_path(arguments.git_executable)
    wheel = Path(arguments.wheel).resolve(strict=True)
    package = _read_package_manifest(
        Path(arguments.package_manifest),
        source_root,
        arguments.candidate,
        wheel,
        str(git),
    )
    invocation = _new_private_invocation(Path(arguments.invocation_path))
    environment = invocation / "venv"
    output = invocation / "native-wakeup-recovery.json"
    succeeded = False
    try:
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(environment)
        python = _venv_python(environment).resolve(strict=True)
        installed = subprocess.run(
            [str(python), "-m", "pip", "install", "--no-cache-dir", str(wheel)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env=_worker_environment(invocation, git, environment),
        )
        if installed.returncode != 0:
            raise RuntimeError("exact candidate wheel installation failed")
        command = [
            str(python),
            str(Path(__file__).resolve(strict=True)),
            "--worker",
            "--candidate",
            package["candidate"],
            "--source-root",
            str(source_root),
            "--git-executable",
            str(git),
            "--wheel",
            str(wheel),
            "--package-manifest",
            str(Path(arguments.package_manifest).resolve(strict=True)),
            "--invocation-root",
            str(invocation),
            "--output",
            str(output),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
            env=_worker_environment(invocation, git, environment),
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"attested native wakeup worker failed: {_worker_failure_detail(completed.stdout)}"
            )
        result = json.loads(output.read_text(encoding="utf-8"))
        provenance = result.get("provenance") if isinstance(result, dict) else None
        if not isinstance(provenance, dict):
            raise RuntimeError("native wakeup provenance is absent")
        _validate_result(result, provenance, package)
        succeeded = True
    finally:
        try:
            if environment.exists():
                shutil.rmtree(environment, ignore_errors=False)
        finally:
            if not succeeded and invocation.exists():
                shutil.rmtree(invocation, ignore_errors=False)
    if environment.exists() or sorted(path.name for path in invocation.iterdir()) != [output.name]:
        raise RuntimeError("native invocation cleanup is incomplete")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--prepare-package", action="store_true")
    parser.add_argument("--verify-result", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source-root")
    parser.add_argument("--git-executable")
    parser.add_argument("--wheel")
    parser.add_argument("--package-manifest")
    parser.add_argument("--invocation-path")
    parser.add_argument("--invocation-root")
    parser.add_argument("--output")
    parser.add_argument("--result")
    parser.add_argument("--child-action", choices=("seed", "crash", "inspect", "recover"))
    parser.add_argument("--root")
    parser.add_argument("--operation", choices=OPERATIONS)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--provenance-digest")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.child_action:
            return _child(arguments)
        if arguments.prepare_package:
            if (
                not arguments.wheel
                or not arguments.package_manifest
                or not arguments.git_executable
            ):
                raise RuntimeError("package provenance inputs are incomplete")
            source_root = Path(__file__).resolve(strict=True).parents[1]
            _write_package_manifest(
                source_root,
                arguments.candidate,
                Path(arguments.wheel),
                Path(arguments.package_manifest),
                arguments.git_executable,
            )
            return 0
        if arguments.verify_result:
            return _verify_existing_result(arguments)
        if arguments.worker:
            return _worker(arguments)
        return _bootstrap(arguments)
    except Exception as exc:  # noqa: BLE001 - one bounded public report
        # A child is always captured by its parent and must not leak a traceback
        # containing temporary paths.  The public runner emits one bounded report.
        if arguments.child_action:
            return 1
        failure: dict[str, object] = {
            "ok": False,
            "candidate": "unverified",
            "platform": (
                "windows"
                if os.name == "nt"
                else ("macos" if sys.platform == "darwin" else "unsupported")
            ),
            "reason": "native-wakeup-recovery-proof-failed",
        }
        # This probe raises RuntimeError with a fixed, curated string naming the
        # contract it rejected, never an interpolated path or host value. Any
        # other exception contributes only its class name, which is still enough
        # to tell a failing host apart from a rejected contract.
        failure["detail"] = (
            str(exc)[:200] if isinstance(exc, RuntimeError) and str(exc) else type(exc).__name__
        )
        sys.stdout.write(json.dumps(failure, sort_keys=True) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
