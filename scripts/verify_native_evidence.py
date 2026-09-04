"""Fail closed unless native evidence is complete, bound, and upload-safe."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from native_evidence import SHA1_RE, canonical_sha256

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_PATH_RE = re.compile(r"(?:^[A-Za-z]:[\\/]|[/\\](?:Users|home)[/\\])")
INSTALL_STEPS = (
    "provenance",
    "harness",
    "manager-definition",
    "wire",
    "restoration",
)
SERVICE_STEPS = (
    "provenance",
    "manager-identity",
    "endpoint-contract",
    "adapter-wired",
    "installed",
    "stopped",
    "started",
    "restarted",
    "manager-recovered-owned-process",
    "boundary-prepared",
    "boundary-verified",
    "configuration-restored",
    "teardown",
)


class VerificationFailure(RuntimeError):
    pass


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _walk_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _walk_strings(nested)]
    return []


def _verify_provenance(provenance: dict[str, Any], candidate: str) -> str:
    binding = str(provenance.get("binding_sha256", ""))
    bound = {
        "source": provenance.get("source"),
        "distribution": provenance.get("distribution"),
        "runtime_tools": provenance.get("runtime_tools"),
    }
    if provenance.get("schema") != "brains.native-provenance.v1":
        raise VerificationFailure("native provenance schema differs")
    if binding != canonical_sha256(bound):
        raise VerificationFailure("native provenance binding digest differs")
    source = bound["source"]
    if not isinstance(source, dict) or source.get("commit") != candidate:
        raise VerificationFailure("native provenance candidate differs")
    for key in ("tree", "git_sha256"):
        if not SHA1_RE.fullmatch(str(source.get(key, ""))) and not SHA256_RE.fullmatch(
            str(source.get(key, ""))
        ):
            raise VerificationFailure("native source provenance hash differs")
    distribution = bound["distribution"]
    try:
        wheel = distribution["wheel"]
        installed = distribution["installed"]
    except (KeyError, TypeError) as exc:
        raise VerificationFailure("native distribution provenance is incomplete") from exc
    required_hashes = (
        wheel.get("sha256"),
        wheel.get("payload_manifest_sha256"),
        installed.get("manifest_sha256"),
        installed.get("metadata_sha256"),
        installed.get("direct_url_sha256"),
        installed.get("executable_sha256"),
        installed.get("interpreter_sha256"),
    )
    if not all(SHA256_RE.fullmatch(str(value)) for value in required_hashes):
        raise VerificationFailure("native distribution provenance hash is incomplete")
    if int(installed.get("record_hashes_verified", 0)) <= 0:
        raise VerificationFailure("installed RECORD verification is absent")
    return binding


def verify_record(
    path: Path,
    *,
    kind: str,
    candidate: str,
    manager: str,
    python: str,
    adapter: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerificationFailure("native evidence input is not a regular file")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationFailure("native evidence JSON is unreadable") from exc
    if result.get("passed") is not True:
        raise VerificationFailure("native evidence did not pass")
    expected_schema = f"brains.native-{kind}-evidence.v1"
    if result.get("schema") != expected_schema:
        raise VerificationFailure("native evidence schema differs")
    expected_matrix = {
        "manager": manager,
        "python": python,
        "adapter": adapter,
        "transport": "streamable-http",
    }
    if result.get("matrix") != expected_matrix:
        raise VerificationFailure("native evidence matrix identity differs")
    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        raise VerificationFailure("native evidence provenance is absent")
    binding = _verify_provenance(provenance, candidate)
    steps = result.get("steps")
    required_steps = INSTALL_STEPS if kind == "installation" else SERVICE_STEPS
    if result.get("phase") == "cleanup" and kind == "service":
        cleanup = result.get("cleanup", {})
        if not all(
            cleanup.get(key) is True
            for key in ("definition_removed", "listeners_removed", "runtime_root_removed")
        ):
            raise VerificationFailure("native cleanup evidence is incomplete")
    else:
        if not isinstance(steps, list) or tuple(row.get("step") for row in steps) != required_steps:
            raise VerificationFailure("native evidence step sequence differs")
        if any(
            row.get("passed") is not True
            or row.get("provenance_sha256") != binding
            or row.get("sequence") != index
            for index, row in enumerate(steps, start=1)
        ):
            raise VerificationFailure("native evidence step binding differs")
    if any(HOST_PATH_RE.search(value) for value in _walk_strings(result)):
        raise VerificationFailure("native evidence contains a host path")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("installation", "service"), required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--manager", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        if not SHA1_RE.fullmatch(args.candidate):
            raise VerificationFailure("candidate must be a full commit id")
        records = [
            verify_record(
                path,
                kind=args.kind,
                candidate=args.candidate,
                manager=args.manager,
                python=args.python,
                adapter=args.adapter,
            )
            for path in args.input
        ]
        if (
            len(records) > 1
            and len({record["provenance"]["binding_sha256"] for record in records}) != 1
        ):
            raise VerificationFailure("native evidence files have different provenance")
    except Exception:  # noqa: BLE001 - verifier emits no artifact content
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
