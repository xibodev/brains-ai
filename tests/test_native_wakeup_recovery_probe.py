"""Static safety contract for the native Claude wakeup recovery probe."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "probe_native_wakeup_recovery.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
COMMIT = "0" * 40
DIGEST = "1" * 64


def _module():
    spec = importlib.util.spec_from_file_location("native_wakeup_probe_contract", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_uses_real_abrupt_process_boundaries_and_complete_matrix() -> None:
    probe = _module()
    source = PROBE.read_text(encoding="utf-8")

    assert probe.PHASES == ("prepared", "swapped", "validated", "metadata")
    assert probe.OPERATIONS == ("install", "remove")
    assert "os._exit(CRASH_EXIT_CODE)" in source
    assert "ReplaceFileW" in source
    assert "renamex_np(RENAME_SWAP)" in source
    assert '"displaced-settings.bin"' in source
    assert "Never relay child output" in source
    assert "installed_record_sha256" in source
    assert "launcher_sha256" in source
    assert "interpreter_sha256" in source
    assert "native recovery directory survived" in source
    assert '_home_snapshot(paths["home"]) != _read_baseline(root)' in source
    assert '"listeners_started": False' in source


def test_probe_child_environment_replaces_ambient_brains_and_home_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _module()
    for key in (
        "home",
        "state",
        "tmp",
    ):
        (tmp_path / key).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRAINS_DB_URL", "sqlite:///ambient-must-not-survive.db")
    monkeypatch.setenv("BRAINS_STATE_DIR", "ambient-must-not-survive")
    monkeypatch.setenv("BRAINS_UNRELATED_AMBIENT_VALUE", "must-not-survive")
    monkeypatch.setenv("HOME", "ambient-home-must-not-survive")
    monkeypatch.setenv("USERPROFILE", "ambient-home-must-not-survive")
    monkeypatch.setenv("PATH", "ambient-shadow-path-must-not-survive")

    environment = probe._isolated_environment(tmp_path, COMMIT, DIGEST)

    assert environment["BRAINS_NATIVE_WAKEUP_PROBE_CANDIDATE"] == COMMIT
    assert environment["BRAINS_NATIVE_WAKEUP_PROBE_PROVENANCE_DIGEST"] == DIGEST
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["USERPROFILE"] == str(tmp_path / "home")
    assert environment["BRAINS_STATE_DIR"] == str(tmp_path / "state")
    assert environment["BRAINS_DB_URL"].endswith("/state/brains.db")
    assert "BRAINS_UNRELATED_AMBIENT_VALUE" not in environment
    assert "ambient-shadow-path" not in environment["PATH"]
    assert all(
        not value.startswith("ambient-")
        for key, value in environment.items()
        if key in probe._ISOLATED_ENV_KEYS
    )
    with pytest.raises(RuntimeError, match="escaped"):
        probe._owned(tmp_path.parent / "outside", tmp_path)


def test_native_matrix_and_real_claude_probe_are_blocking() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate = workflow.split("\n  gate:\n", 1)[1]

    assert "scripts/probe_native_wakeup_recovery.py" in workflow
    assert "runner.os == 'Windows' || runner.os == 'macOS'" in workflow
    assert '--candidate "${{ github.sha }}"' in workflow
    assert "--package-manifest dist/native-wakeup-package-provenance.json" in workflow
    assert "--invocation-path $invocation" in workflow
    assert "steps.native-wakeup-evidence.outputs.evidence_path" in workflow
    assert "--git-executable $git" in workflow
    assert "--github-output" not in workflow
    assert "if: success() &&" in workflow
    assert "if: always() && (runner.os == 'Windows' || runner.os == 'macOS')" not in workflow
    assert "steps.native-environment.outputs.python" in workflow
    assert "if-no-files-found: error" in workflow
    assert "      - claude-wakeup-probe\n" in gate


def test_public_report_is_bounded_and_cannot_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _module()
    report = {
        "ok": True,
        "candidate": COMMIT,
        "platform": "synthetic",
        "scenarios": len(probe.PHASES) * len(probe.OPERATIONS),
    }

    output = tmp_path / "native-result.json"
    probe._write_public_result(output, report)

    rendered = output.read_text(encoding="utf-8")
    assert json.loads(rendered) == report
    assert capsys.readouterr().out == rendered
    assert str(tmp_path) not in rendered
    assert "settings" not in rendered
    assert "S-1-" not in rendered
    with pytest.raises(RuntimeError, match="already exists"):
        probe._write_public_result(output, report)


def test_candidate_and_wheel_manifest_mismatches_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _module()
    wheel = tmp_path / "brains_ai-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("brains_ai-1.dist-info/RECORD", "brains/__init__.py,,\n")
        archive.writestr("brains_ai-1.dist-info/METADATA", "Name: brains-ai\n")
        archive.writestr("brains_ai-1.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    git = tmp_path / "git"
    git.write_bytes(b"synthetic-git-executable")
    output = tmp_path / "package.json"
    identity = {"candidate": COMMIT, "source_tree": "2" * 40}
    monkeypatch.setattr(probe, "_source_identity", lambda *_args: identity)

    manifest = probe._write_package_manifest(tmp_path, COMMIT, wheel, output, str(git))
    assert manifest["candidate"] == COMMIT
    assert len(manifest["wheel_sha256"]) == 64
    assert probe._read_package_manifest(output, tmp_path, COMMIT, wheel, str(git)) == manifest

    wheel.write_bytes(wheel.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="does not match"):
        probe._read_package_manifest(output, tmp_path, COMMIT, wheel, str(git))
    with pytest.raises(RuntimeError, match="already exists"):
        probe._write_package_manifest(tmp_path, COMMIT, wheel, output, str(git))


def test_arbitrary_full_sha_cannot_claim_checked_out_candidate(tmp_path: Path) -> None:
    probe = _module()
    git = shutil.which("git")
    assert git is not None
    repository = tmp_path / "source"
    repository.mkdir()
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    for arguments in (
        ("init",),
        ("config", "--local", "user.name", "Native Proof"),
        ("config", "--local", "user.email", "native-proof@example.invalid"),
    ):
        subprocess.run(
            [git, *arguments],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    (repository / "tracked.txt").write_text("synthetic source\n", encoding="utf-8")
    subprocess.run(
        [git, "add", "tracked.txt"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [git, "commit", "-m", "synthetic-source"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(RuntimeError, match="does not match"):
        probe._source_identity(repository, COMMIT, str(Path(git).resolve()))


def test_git_and_interpreter_provenance_reject_ambient_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _module()
    with pytest.raises(RuntimeError, match="absolute path"):
        probe._git_path("git")
    monkeypatch.setattr(probe.importlib.metadata, "distribution", lambda _name: object())
    monkeypatch.setattr(probe.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(probe.sys, "base_prefix", str(tmp_path))
    with pytest.raises(RuntimeError, match="dedicated virtual environment"):
        probe._runtime_provenance({}, tmp_path / "candidate.whl")


def test_whole_synthetic_home_snapshot_detects_extra_or_changed_entries(tmp_path: Path) -> None:
    probe = _module()
    home = tmp_path / "home"
    nested = home / ".claude" / "profiles"
    nested.mkdir(parents=True)
    marker = nested / "marker.bin"
    marker.write_bytes(b"before")
    before = probe._home_snapshot(home)
    marker.write_bytes(b"after")
    assert probe._home_snapshot(home) != before
    marker.write_bytes(b"before")
    (home / ".claude" / "stale-recovery").mkdir()
    assert probe._home_snapshot(home) != before


def test_private_invocation_is_new_and_owner_only(tmp_path: Path) -> None:
    probe = _module()
    invocation = probe._new_private_invocation(tmp_path / "new-invocation")
    assert invocation.parent == tmp_path
    assert not any(invocation.iterdir())
    if probe.os.name != "nt":
        assert probe.stat.S_IMODE(invocation.stat().st_mode) == 0o700
    with pytest.raises(RuntimeError, match="already exists"):
        probe._new_private_invocation(invocation)


@pytest.mark.parametrize("operation", ["install", "remove"])
@pytest.mark.parametrize("phase", ["prepared", "swapped", "validated", "metadata"])
def test_recovery_restores_exact_post_wire_home_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    phase: str,
) -> None:
    probe = _module()
    root = tmp_path / operation
    root.mkdir()
    for key in list(probe.os.environ):
        if key.startswith("BRAINS_"):
            monkeypatch.delenv(key, raising=False)
    environment = probe._isolated_environment(root, COMMIT, DIGEST)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    from brains import wire as real_wire

    def exchange_files(target: Path, replacement: Path) -> None:
        target_bytes = target.read_bytes()
        replacement_bytes = replacement.read_bytes()
        target.write_bytes(replacement_bytes)
        replacement.write_bytes(target_bytes)

    monkeypatch.setattr(real_wire, "_exchange_files", exchange_files)
    monkeypatch.setattr(real_wire, "_harden", lambda _path: None)
    monkeypatch.setattr(real_wire, "secure_owner_only_directory", lambda _path: None)
    monkeypatch.setattr(real_wire, "secure_owner_only_file", lambda _path: None)
    monkeypatch.setattr(
        real_wire,
        "protect_owner_only_bytes",
        lambda data: ("posix-owner", data),
    )
    monkeypatch.setattr(
        real_wire,
        "unprotect_owner_only_bytes",
        lambda protection, data: data if protection == "posix-owner" else None,
    )
    monkeypatch.setattr(real_wire, "_TRANSACTION_PHASE_HOOK", None)

    probe._seed(root, COMMIT, DIGEST, operation)
    baseline = probe._read_baseline(root)
    assert baseline[".claude.json"] == probe._home_snapshot(root / "home")[".claude.json"]

    paths = probe._paths(root)
    if operation == "remove":
        assert probe._home_snapshot(paths["home"]) != baseline

    class SimulatedCrash(RuntimeError):
        pass

    def crash_at_phase(name: str) -> None:
        if name == phase:
            raise SimulatedCrash(phase)

    adapter = probe._claude_adapter(real_wire, paths["home"])
    monkeypatch.setattr(real_wire, "_TRANSACTION_PHASE_HOOK", crash_at_phase)
    with pytest.raises(SimulatedCrash, match=phase):
        if operation == "install":
            real_wire._wire_wakeup(adapter, paths["home"], False)
        else:
            real_wire._unwire_wakeup(adapter, paths["home"], False)
    monkeypatch.setattr(real_wire, "_TRANSACTION_PHASE_HOOK", None)

    probe._recover(root, COMMIT, DIGEST, operation)

    assert probe._home_snapshot(paths["home"]) == baseline
    assert not paths["recovery"].exists()
