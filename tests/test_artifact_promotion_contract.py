"""Contract tests for exact-artifact promotion and provenance binding (Issue #41)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.artifact_promotion import (
    SCHEMA,
    PromotionError,
    create_provenance_manifest,
    file_sha256,
    update_oci_digest,
    verify_provenance_manifest,
)


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create a temporary git repo with one commit and return (repo_path, git_path, commit_sha, tree_sha)."""
    git = shutil.which("git")
    assert git is not None, "git must be available for testing"
    git_path = Path(git)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        [str(git_path), "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )

    dummy_file = repo / "README.md"
    dummy_file.write_text("hello brains\n", encoding="utf-8")
    subprocess.run(
        [str(git_path), "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        [str(git_path), "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )

    commit = (
        subprocess.run(
            [str(git_path), "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .lower()
    )

    tree = (
        subprocess.run(
            [str(git_path), "-C", str(repo), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .lower()
    )

    return repo, git_path, commit, tree


def _make_dummy_wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("brains_ai/__init__.py", "__version__ = '1.6.0'\n")
        record_content = "brains_ai/__init__.py,sha256=xxx,25\n"
        z.writestr("brains_ai-1.6.0.dist-info/RECORD", record_content)


def _make_dummy_sdist(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tar:
        data = b"dummy sdist content\n"
        import io

        info = tarfile.TarInfo(name="brains_ai-1.6.0/README.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_create_and_verify_provenance_manifest_success(
    synthetic_repo: tuple[Path, Path, str, str],
) -> None:
    repo, git_path, commit, tree = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
        oci_digest="sha256:" + "a" * 64,
        oci_tag=f"candidate-{commit}",
    )

    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    assert manifest_data["schema"] == SCHEMA
    assert manifest_data["candidate"] == commit
    assert manifest_data["source_tree"] == tree
    assert manifest_data["artifacts"]["wheel"]["filename"] == wheel.name
    assert manifest_data["artifacts"]["wheel"]["sha256"] == file_sha256(wheel)
    assert manifest_data["artifacts"]["sdist"]["filename"] == sdist.name
    assert manifest_data["artifacts"]["sdist"]["sha256"] == file_sha256(sdist)
    assert manifest_data["artifacts"]["container"]["oci_manifest_digest"] == "sha256:" + "a" * 64

    # Verification must succeed
    verified = verify_provenance_manifest(
        dist_dir=dist,
        expected_candidate=commit,
        manifest_path=manifest_file,
        repo_root=repo,
        git_executable=git_path,
        require_oci=True,
        expected_oci_digest="sha256:" + "a" * 64,
    )
    assert verified["candidate"] == commit


def test_tampered_wheel_fails_verification(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Tamper with wheel
    wheel.write_bytes(wheel.read_bytes() + b"\x00corrupt")

    with pytest.raises(PromotionError, match="wheel sha256 mismatch"):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_tampered_sdist_fails_verification(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Tamper with sdist
    sdist.write_bytes(sdist.read_bytes() + b"\x00corrupt")

    with pytest.raises(PromotionError, match="sdist sha256 mismatch"):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_mismatched_candidate_commit_fails(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    wrong_commit = "0" * 40
    with pytest.raises(PromotionError, match="does not match expected"):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=wrong_commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_mismatched_source_tree_fails(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    # Alter source tree in manifest
    manifest_data["source_tree"] = "f" * 40
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    with pytest.raises(PromotionError, match="manifest tree .* does not match current tree"):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_missing_artifacts_fail(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Remove wheel
    wheel.unlink()
    with pytest.raises(PromotionError, match="dist directory must contain exactly one wheel"):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_unexpected_extra_artifact_fails(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Add an unexpected extra wheel
    extra_wheel = dist / "rogue_package-1.0-py3-none-any.whl"
    _make_dummy_wheel(extra_wheel)

    with pytest.raises(
        PromotionError, match="dist directory must contain exactly one wheel, found 2"
    ):
        verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
        )


def test_update_oci_digest_and_recovery(synthetic_repo: tuple[Path, Path, str, str]) -> None:
    repo, git_path, commit, _ = synthetic_repo
    dist = repo / "dist"
    dist.mkdir()

    wheel = dist / "brains_ai-1.6.0-py3-none-any.whl"
    sdist = dist / "brains_ai-1.6.0.tar.gz"
    _make_dummy_wheel(wheel)
    _make_dummy_sdist(sdist)

    manifest_data = create_provenance_manifest(
        dist_dir=dist,
        candidate_commit=commit,
        repo_root=repo,
        git_executable=git_path,
    )
    manifest_file = dist / "artifact-provenance.json"
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Update with OCI digest
    digest = "sha256:" + "b" * 64
    update_oci_digest(manifest_file, digest)

    verified = verify_provenance_manifest(
        dist_dir=dist,
        expected_candidate=commit,
        manifest_path=manifest_file,
        repo_root=repo,
        git_executable=git_path,
        require_oci=True,
        expected_oci_digest=digest,
    )
    assert verified["artifacts"]["container"]["oci_manifest_digest"] == digest

    # Verify idempotency and retry
    for _ in range(3):
        res = verify_provenance_manifest(
            dist_dir=dist,
            expected_candidate=commit,
            manifest_path=manifest_file,
            repo_root=repo,
            git_executable=git_path,
            require_oci=True,
            expected_oci_digest=digest,
        )
        assert res["candidate"] == commit
