"""Shared provenance primitives for disposable native evidence probes.

The public artifacts produced by the native probes contain hashes and bounded
logical identifiers only.  Absolute paths, account names, command output, and
configuration contents deliberately remain on the disposable runner.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import urllib.parse
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any


class ProvenanceFailure(RuntimeError):
    pass


SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
BACKUP_RE = re.compile(r"\.bak-[0-9]{8}-[0-9]{6}$")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(git_executable: Path, repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [str(git_executable), "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ProvenanceFailure("candidate repository validation failed")
    return completed.stdout.strip()


def source_provenance(repo: Path, candidate: str, git_executable: Path) -> dict[str, str]:
    """Bind a full candidate id to the clean checked-out commit and tree."""
    normalized = candidate.casefold()
    if not SHA1_RE.fullmatch(normalized):
        raise ProvenanceFailure("candidate must be a full Git SHA-1 commit id")
    git = git_executable.resolve(strict=True)
    if not git.is_file() or not git.name.casefold().startswith("git"):
        raise ProvenanceFailure("explicit Git executable identity differs")
    head = _git(git, repo, "rev-parse", "--verify", "HEAD^{commit}").casefold()
    resolved = _git(git, repo, "rev-parse", "--verify", f"{normalized}^{{commit}}").casefold()
    if resolved != normalized or head != normalized:
        raise ProvenanceFailure("candidate does not equal the checked-out commit")
    if _git(git, repo, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ProvenanceFailure("checked-out candidate has tracked modifications")
    tree = _git(git, repo, "rev-parse", "--verify", "HEAD^{tree}").casefold()
    return {"commit": normalized, "tree": tree, "git_sha256": file_sha256(git)}


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _direct_url_wheel(
    distribution: importlib.metadata.Distribution,
    wheel: Path,
    wheel_sha256: str,
) -> str:
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise ProvenanceFailure("installed distribution has no direct wheel provenance")
    try:
        direct = json.loads(raw)
        raw_url = str(direct["url"])
        archive_info = direct["archive_info"]
        if not isinstance(archive_info, dict):
            raise TypeError("archive_info")
        archive_hash = str(
            archive_info.get("hash") or f"sha256={archive_info.get('hashes', {}).get('sha256', '')}"
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProvenanceFailure("installed direct wheel provenance is malformed") from exc
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme != "file":
        raise ProvenanceFailure("installed distribution did not originate from a local wheel")
    installed_from = Path(urllib.parse.unquote(parsed.path))
    if os.name == "nt" and installed_from.as_posix().startswith("/"):
        installed_from = Path(installed_from.as_posix()[1:])
    if installed_from.resolve() != wheel:
        raise ProvenanceFailure("installed distribution references a different wheel")
    if archive_hash.casefold() != f"sha256={wheel_sha256}":
        raise ProvenanceFailure("installed distribution wheel hash does not match")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _wheel_payload(wheel: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    manifest: list[dict[str, Any]] = []
    payload: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(wheel) as archive:
            for info in sorted(archive.infolist(), key=lambda row: row.filename):
                if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                    continue
                relative = PurePosixPath(info.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ProvenanceFailure("wheel contains an unsafe member path")
                content = archive.read(info)
                payload[relative.as_posix()] = content
                manifest.append(
                    {
                        "path": relative.as_posix(),
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProvenanceFailure("candidate wheel is unreadable") from exc
    if not manifest:
        raise ProvenanceFailure("candidate wheel has no verifiable payload")
    return manifest, payload


def distribution_provenance(
    wheel_path: Path,
    executable_path: Path,
    *,
    distribution_name: str = "brains-ai",
) -> dict[str, Any]:
    """Prove that the active interpreter/executable came from the exact wheel."""
    wheel = wheel_path.resolve(strict=True)
    executable_entry = executable_path.absolute()
    interpreter_entry = Path(sys.executable).absolute()
    prefix = Path(sys.prefix).resolve(strict=True)
    if not _within(interpreter_entry, prefix) or not _within(executable_entry, prefix):
        raise ProvenanceFailure("probe interpreter or executable is outside its environment")
    if sys.prefix == sys.base_prefix:
        raise ProvenanceFailure("native evidence requires a fresh virtual environment")
    executable = executable_entry.resolve(strict=True)
    interpreter = interpreter_entry.resolve(strict=True)

    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ProvenanceFailure("installed brains-ai distribution is absent") from exc
    if distribution.metadata.get("Name", "").casefold().replace("_", "-") != distribution_name:
        raise ProvenanceFailure("installed distribution identity differs")
    entry_points = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    if entry_points.get("brains-ai") != "brains.cli.app:app":
        raise ProvenanceFailure("installed console entry point differs")

    wheel_sha256 = file_sha256(wheel)
    direct_url_sha256 = _direct_url_wheel(distribution, wheel, wheel_sha256)
    wheel_manifest, wheel_payload = _wheel_payload(wheel)
    for relative, expected in wheel_payload.items():
        installed = Path(distribution.locate_file(relative))
        if not installed.is_file() or installed.read_bytes() != expected:
            raise ProvenanceFailure("installed payload differs from the candidate wheel")

    installed_manifest: list[dict[str, Any]] = []
    record_hashes_verified = 0
    for item in sorted(distribution.files or (), key=lambda row: str(row)):
        located = Path(distribution.locate_file(item)).resolve()
        if not located.is_file():
            continue
        if not _within(located, prefix):
            raise ProvenanceFailure("installed distribution reports a path outside its environment")
        relative = located.relative_to(prefix).as_posix()
        digest = file_sha256(located)
        recorded_hash = getattr(item, "hash", None)
        if recorded_hash is not None:
            if recorded_hash.mode != "sha256":
                raise ProvenanceFailure("installed RECORD uses an unsupported hash")
            encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode("ascii")
            if recorded_hash.value != encoded:
                raise ProvenanceFailure("installed file differs from its RECORD hash")
            record_hashes_verified += 1
        recorded_size = getattr(item, "size", None)
        if recorded_size is not None and int(recorded_size) != located.stat().st_size:
            raise ProvenanceFailure("installed file differs from its RECORD size")
        installed_manifest.append(
            {
                "path": relative,
                "size": located.stat().st_size,
                "sha256": digest,
            }
        )
    if not installed_manifest:
        raise ProvenanceFailure("installed distribution manifest is empty")
    if record_hashes_verified < len(wheel_payload):
        raise ProvenanceFailure("installed RECORD does not cover the wheel payload")

    metadata_hashes: dict[str, str] = {}
    for name in ("METADATA", "WHEEL", "entry_points.txt", "direct_url.json"):
        content = distribution.read_text(name)
        if content is not None:
            metadata_hashes[name.casefold().replace(".", "_")] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
    return {
        "wheel": {
            "sha256": wheel_sha256,
            "size": wheel.stat().st_size,
            "payload_manifest_sha256": canonical_sha256(wheel_manifest),
        },
        "installed": {
            "name": distribution_name,
            "version": distribution.version,
            "manifest_sha256": canonical_sha256(installed_manifest),
            "metadata_sha256": canonical_sha256(metadata_hashes),
            "direct_url_sha256": direct_url_sha256,
            "record_hashes_verified": record_hashes_verified,
            "executable_sha256": file_sha256(executable),
            "interpreter_sha256": file_sha256(interpreter),
            "console_entry_point": "brains.cli.app:app",
        },
    }


def create_provenance(
    *,
    candidate: str,
    repo: Path,
    wheel: Path,
    executable: Path,
    git_executable: Path,
    runtime_tools: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    bound = {
        "source": source_provenance(repo.resolve(strict=True), candidate, git_executable),
        "distribution": distribution_provenance(wheel, executable),
        "runtime_tools": runtime_tools or {},
    }
    return {
        "schema": "brains.native-provenance.v1",
        "binding_sha256": canonical_sha256(bound),
        **bound,
    }


def explicit_runtime_tools(
    raw_json: str,
    *,
    required: Iterable[str],
    prepend_paths: Iterable[Path] = (),
) -> tuple[dict[str, dict[str, Any]], str]:
    """Validate explicit tool paths and return sanitized hashes plus a closed PATH."""
    try:
        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceFailure("explicit native tool map is malformed") from exc
    if not isinstance(raw, dict) or set(raw) != set(required):
        raise ProvenanceFailure("explicit native tool map differs from the required set")
    record: dict[str, dict[str, Any]] = {}
    directories = [path.resolve(strict=True) for path in prepend_paths]
    for name in sorted(raw):
        supplied = Path(str(raw[name]))
        if not supplied.is_absolute():
            raise ProvenanceFailure("native tool path is not absolute")
        executable = supplied.resolve(strict=True)
        if not executable.is_file() or not executable.name.casefold().startswith(name.casefold()):
            raise ProvenanceFailure("native tool executable identity differs")
        record[name] = {
            "executable": executable.name,
            "sha256": file_sha256(executable),
        }
        directories.append(executable.parent)
    unique: list[str] = []
    for directory in directories:
        rendered = str(directory)
        if rendered not in unique:
            unique.append(rendered)
    return record, os.pathsep.join(unique)


def snapshot_files(roots: Iterable[tuple[str, Path]]) -> dict[str, dict[str, Any]]:
    """Hash every file below logical roots without exposing host paths or contents."""
    snapshot: dict[str, dict[str, Any]] = {}
    for logical, root in roots:
        if root.is_symlink():
            raise ProvenanceFailure("synthetic configuration root may not be a symlink")
        if root.is_file():
            snapshot[logical] = {"size": root.stat().st_size, "sha256": file_sha256(root)}
            continue
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ProvenanceFailure("synthetic configuration tree may not contain symlinks")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            snapshot[f"{logical}/{relative}"] = {
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
    return snapshot


def account_managed_backups(
    baseline: dict[str, dict[str, Any]],
    wired: dict[str, dict[str, Any]],
    restored: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Require exact primary restoration and classify only timestamped wire backups."""
    for key, expected in baseline.items():
        if restored.get(key) != expected:
            raise ProvenanceFailure("primary client configuration was not exactly restored")
    extras = {key: value for key, value in restored.items() if key not in baseline}
    allowed_hashes = {
        str(value["sha256"]) for value in (*baseline.values(), *wired.values()) if "sha256" in value
    }
    for key, value in extras.items():
        if not BACKUP_RE.search(PurePosixPath(key).name):
            raise ProvenanceFailure("unexpected managed configuration artifact remains")
        if value.get("sha256") not in allowed_hashes:
            raise ProvenanceFailure("managed backup does not preserve a known lifecycle state")
    return extras


def require_fresh_output(output: Path) -> None:
    for path in (output, output.with_suffix(".xml")):
        if path.exists() or path.is_symlink():
            raise ProvenanceFailure("native evidence output already exists")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def assert_sanitized(value: Any, forbidden: Iterable[str]) -> None:
    encoded = json.dumps(value, sort_keys=True).casefold()
    for item in forbidden:
        normalized = item.strip().casefold()
        if normalized and normalized in encoded:
            raise ProvenanceFailure("native evidence contains a forbidden host value")
