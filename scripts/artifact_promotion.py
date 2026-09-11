"""Exact-artifact qualification and release promotion binding.

Binds qualification to source commit, source tree, wheel, sdist, and OCI
manifest identities together so release promotion publishes the exact qualified
artifacts without rebuilding.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

SCHEMA = "brains-artifact-provenance/v1"
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PromotionError(RuntimeError):
    """Raised when artifact promotion qualification or verification fails."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _run_git(git_path: Path, repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [str(git_path), "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise PromotionError(f"git command failed: git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def inspect_wheel(wheel: Path) -> dict[str, str]:
    if not wheel.is_file():
        raise PromotionError(f"wheel is not a file: {wheel}")
    with zipfile.ZipFile(wheel) as archive:
        record_names = [n for n in archive.namelist() if n.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise PromotionError("wheel RECORD member is missing or ambiguous")
        record_data = archive.read(record_names[0]).decode("utf-8")
        rows = sorted(csv.reader(record_data.splitlines()))
        record_hash = canonical_sha256(rows)
    return {
        "filename": wheel.name,
        "sha256": file_sha256(wheel),
        "record_sha256": record_hash,
    }


def inspect_sdist(sdist: Path) -> dict[str, str]:
    if not sdist.is_file():
        raise PromotionError(f"sdist is not a file: {sdist}")
    return {
        "filename": sdist.name,
        "sha256": file_sha256(sdist),
    }


def create_provenance_manifest(
    *,
    dist_dir: Path,
    candidate_commit: str,
    repo_root: Path,
    git_executable: Path,
    oci_digest: str | None = None,
    oci_tag: str | None = None,
    oci_registry: str = "ghcr.io",
    oci_repository: str = "xibodev/brains-ai",
) -> dict[str, Any]:
    resolved_repo = repo_root.resolve(strict=True)
    resolved_git = git_executable.resolve(strict=True)

    head = _run_git(
        resolved_git, resolved_repo, "rev-parse", "--verify", "HEAD^{commit}"
    ).casefold()
    tree = _run_git(resolved_git, resolved_repo, "rev-parse", "--verify", "HEAD^{tree}").casefold()
    candidate = candidate_commit.strip().casefold()

    if head != candidate:
        raise PromotionError(f"candidate commit {candidate} does not match HEAD {head}")
    if not SHA1_RE.fullmatch(head) or not SHA1_RE.fullmatch(tree):
        raise PromotionError(f"invalid git commit or tree hash: commit={head}, tree={tree}")

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))

    if len(wheels) != 1:
        raise PromotionError(f"expected exactly one wheel in {dist_dir}, found {len(wheels)}")
    if len(sdists) != 1:
        raise PromotionError(f"expected exactly one sdist in {dist_dir}, found {len(sdists)}")

    wheel_info = inspect_wheel(wheels[0])
    sdist_info = inspect_sdist(sdists[0])

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate": head,
        "source_tree": tree,
        "builder_git_sha256": file_sha256(resolved_git),
        "artifacts": {
            "wheel": wheel_info,
            "sdist": sdist_info,
        },
    }

    if oci_digest:
        norm_digest = oci_digest.strip().lower()
        if not OCI_DIGEST_RE.fullmatch(norm_digest):
            raise PromotionError(f"invalid OCI manifest digest: {oci_digest}")
        manifest["artifacts"]["container"] = {
            "registry": oci_registry,
            "repository": oci_repository,
            "candidate_tag": oci_tag or f"candidate-{head}",
            "oci_manifest_digest": norm_digest,
            "platforms": ["linux/amd64", "linux/arm64"],
        }

    return manifest


def verify_provenance_manifest(
    *,
    dist_dir: Path,
    expected_candidate: str,
    manifest_path: Path,
    repo_root: Path,
    git_executable: Path,
    require_oci: bool = False,
    expected_oci_digest: str | None = None,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise PromotionError(f"provenance manifest not found: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError(f"failed to read provenance manifest: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise PromotionError(
            f"unrecognized or missing schema in provenance manifest: {manifest.get('schema')}"
        )

    candidate = str(manifest.get("candidate", "")).casefold()
    expected_norm = expected_candidate.strip().casefold()
    if candidate != expected_norm:
        raise PromotionError(
            f"manifest candidate {candidate} does not match expected {expected_norm}"
        )

    resolved_repo = repo_root.resolve(strict=True)
    resolved_git = git_executable.resolve(strict=True)
    current_tree = _run_git(
        resolved_git, resolved_repo, "rev-parse", "--verify", "HEAD^{tree}"
    ).casefold()
    manifest_tree = str(manifest.get("source_tree", "")).casefold()
    if current_tree != manifest_tree:
        raise PromotionError(
            f"manifest tree {manifest_tree} does not match current tree {current_tree}"
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PromotionError("manifest artifacts map is missing or malformed")

    wheel_entry = artifacts.get("wheel")
    if (
        not isinstance(wheel_entry, dict)
        or "filename" not in wheel_entry
        or "sha256" not in wheel_entry
    ):
        raise PromotionError("manifest wheel artifact entry is missing or malformed")

    sdist_entry = artifacts.get("sdist")
    if (
        not isinstance(sdist_entry, dict)
        or "filename" not in sdist_entry
        or "sha256" not in sdist_entry
    ):
        raise PromotionError("manifest sdist artifact entry is missing or malformed")

    # Check dist directory contents
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))

    if len(wheels) != 1:
        raise PromotionError(f"dist directory must contain exactly one wheel, found {len(wheels)}")
    if len(sdists) != 1:
        raise PromotionError(f"dist directory must contain exactly one sdist, found {len(sdists)}")

    wheel_path = wheels[0]
    sdist_path = sdists[0]

    if wheel_path.name != wheel_entry["filename"]:
        raise PromotionError(
            f"wheel filename mismatch: expected {wheel_entry['filename']}, found {wheel_path.name}"
        )
    actual_wheel_hash = file_sha256(wheel_path)
    if actual_wheel_hash != wheel_entry["sha256"]:
        raise PromotionError(
            f"wheel sha256 mismatch for {wheel_path.name}: expected {wheel_entry['sha256']}, got {actual_wheel_hash}"
        )

    if sdist_path.name != sdist_entry["filename"]:
        raise PromotionError(
            f"sdist filename mismatch: expected {sdist_entry['filename']}, found {sdist_path.name}"
        )
    actual_sdist_hash = file_sha256(sdist_path)
    if actual_sdist_hash != sdist_entry["sha256"]:
        raise PromotionError(
            f"sdist sha256 mismatch for {sdist_path.name}: expected {sdist_entry['sha256']}, got {actual_sdist_hash}"
        )

    # Verify OCI manifest if present or required
    container_entry = artifacts.get("container")
    if require_oci and (
        not isinstance(container_entry, dict) or "oci_manifest_digest" not in container_entry
    ):
        raise PromotionError("required OCI container manifest entry is missing")
    if container_entry is not None:
        if not isinstance(container_entry, dict) or "oci_manifest_digest" not in container_entry:
            raise PromotionError("container artifact entry is malformed")
        digest = str(container_entry["oci_manifest_digest"]).lower()
        if not OCI_DIGEST_RE.fullmatch(digest):
            raise PromotionError(f"invalid OCI manifest digest in container entry: {digest}")
        if expected_oci_digest:
            expected_dig_norm = expected_oci_digest.strip().lower()
            if digest != expected_dig_norm:
                raise PromotionError(
                    f"OCI manifest digest mismatch: expected {expected_dig_norm}, got {digest}"
                )

    return manifest


def update_oci_digest(
    manifest_path: Path, oci_digest: str, oci_tag: str | None = None
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise PromotionError(f"manifest file does not exist: {manifest_path}")
    norm_digest = oci_digest.strip().lower()
    if not OCI_DIGEST_RE.fullmatch(norm_digest):
        raise PromotionError(f"invalid OCI manifest digest: {oci_digest}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = data.get("candidate", "")
    data["artifacts"]["container"] = {
        "registry": "ghcr.io",
        "repository": "xibodev/brains-ai",
        "candidate_tag": oci_tag or f"candidate-{candidate}",
        "oci_manifest_digest": norm_digest,
        "platforms": ["linux/amd64", "linux/arm64"],
    }
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _find_git(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    resolved = shutil.which("git")
    if resolved:
        return Path(resolved)
    raise PromotionError("git executable not found")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create an artifact-provenance manifest")
    create_parser.add_argument("--dist", type=Path, default=Path("dist"))
    create_parser.add_argument("--candidate", required=True, help="Candidate commit SHA")
    create_parser.add_argument("--output", type=Path, default=Path("dist/artifact-provenance.json"))
    create_parser.add_argument("--repo", type=Path, default=Path("."))
    create_parser.add_argument("--git-executable", type=str, default=None)
    create_parser.add_argument("--oci-digest", type=str, default=None)
    create_parser.add_argument("--oci-tag", type=str, default=None)
    create_parser.add_argument("--oci-registry", type=str, default="ghcr.io")
    create_parser.add_argument("--oci-repository", type=str, default="xibodev/brains-ai")

    verify_parser = subparsers.add_parser("verify", help="Verify exact artifact provenance")
    verify_parser.add_argument("--dist", type=Path, default=Path("dist"))
    verify_parser.add_argument("--candidate", required=True, help="Expected candidate commit SHA")
    verify_parser.add_argument(
        "--manifest", type=Path, default=Path("dist/artifact-provenance.json")
    )
    verify_parser.add_argument("--repo", type=Path, default=Path("."))
    verify_parser.add_argument("--git-executable", type=str, default=None)
    verify_parser.add_argument("--require-oci", action="store_true")
    verify_parser.add_argument("--expected-oci-digest", type=str, default=None)

    update_parser = subparsers.add_parser(
        "update-oci", help="Update OCI digest in an existing manifest"
    )
    update_parser.add_argument(
        "--manifest", type=Path, default=Path("dist/artifact-provenance.json")
    )
    update_parser.add_argument("--oci-digest", required=True)
    update_parser.add_argument("--oci-tag", type=str, default=None)

    args = parser.parse_args(argv)

    try:
        git_path = _find_git(getattr(args, "git_executable", None))
        if args.command == "create":
            manifest = create_provenance_manifest(
                dist_dir=args.dist,
                candidate_commit=args.candidate,
                repo_root=args.repo,
                git_executable=git_path,
                oci_digest=args.oci_digest,
                oci_tag=args.oci_tag,
                oci_registry=args.oci_registry,
                oci_repository=args.oci_repository,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"ok: wrote artifact provenance manifest to {args.output}")
            return 0

        if args.command == "verify":
            verify_provenance_manifest(
                dist_dir=args.dist,
                expected_candidate=args.candidate,
                manifest_path=args.manifest,
                repo_root=args.repo,
                git_executable=git_path,
                require_oci=args.require_oci,
                expected_oci_digest=args.expected_oci_digest,
            )
            print("ok: exact qualified artifacts verified against provenance manifest")
            return 0

        if args.command == "update-oci":
            update_oci_digest(args.manifest, args.oci_digest, args.oci_tag)
            print(f"ok: updated OCI manifest digest in {args.manifest}")
            return 0

    except PromotionError as exc:
        print(f"::error::promotion contract error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"::error::unexpected error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
