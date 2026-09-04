from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

_NATIVE_EVIDENCE_PATH = Path(__file__).resolve().parents[1] / "scripts/native_evidence.py"
_NATIVE_EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "brains_test_native_evidence", _NATIVE_EVIDENCE_PATH
)
assert _NATIVE_EVIDENCE_SPEC is not None and _NATIVE_EVIDENCE_SPEC.loader is not None
native_evidence = importlib.util.module_from_spec(_NATIVE_EVIDENCE_SPEC)
_NATIVE_EVIDENCE_SPEC.loader.exec_module(native_evidence)
sys.modules["native_evidence"] = native_evidence
_LIFECYCLE_PATH = Path(__file__).resolve().parents[1] / "scripts/probe_native_service_lifecycle.py"
_LIFECYCLE_SPEC = importlib.util.spec_from_file_location(
    "brains_test_native_lifecycle", _LIFECYCLE_PATH
)
assert _LIFECYCLE_SPEC is not None and _LIFECYCLE_SPEC.loader is not None
native_lifecycle = importlib.util.module_from_spec(_LIFECYCLE_SPEC)
_LIFECYCLE_SPEC.loader.exec_module(native_lifecycle)
_VERIFIER_PATH = Path(__file__).resolve().parents[1] / "scripts/verify_native_evidence.py"
_VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "brains_test_native_verifier", _VERIFIER_PATH
)
assert _VERIFIER_SPEC is not None and _VERIFIER_SPEC.loader is not None
native_verifier = importlib.util.module_from_spec(_VERIFIER_SPEC)
_VERIFIER_SPEC.loader.exec_module(native_verifier)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def test_source_provenance_binds_candidate_to_clean_checked_out_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    tracked = repo / "tracked.txt"
    tracked.write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic Test",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Test",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
    }
    _git(repo, "commit", "-m", "synthetic candidate", env=commit_env)
    candidate = _git(repo, "rev-parse", "HEAD")

    git = Path(shutil.which("git") or "").resolve(strict=True)
    result = native_evidence.source_provenance(repo, candidate, git)

    assert result == {
        "commit": candidate,
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
    }
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.source_provenance(repo, "0" * 40, git)
    tracked.write_text("modified\n", encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.source_provenance(repo, candidate, git)


class _Distribution:
    def __init__(
        self, root: Path, direct_url: str, files: list[importlib.metadata.PackagePath]
    ) -> None:
        self.root = root
        self._direct_url = direct_url
        self.files = files
        self.metadata = {"Name": "brains-ai"}
        self.version = "1.3.1"
        self.entry_points = [
            importlib.metadata.EntryPoint(
                name="brains-ai", value="brains.cli.app:app", group="console_scripts"
            )
        ]

    def locate_file(self, item: str | PurePosixPath) -> Path:
        return self.root / str(item)

    def read_text(self, name: str) -> str | None:
        if name == "direct_url.json":
            return self._direct_url
        candidate = self.root / "brains_ai-1.3.1.dist-info" / name
        return candidate.read_text(encoding="utf-8") if candidate.is_file() else None


def _installed_fixture(tmp_path: Path) -> tuple[Path, Path, Path, _Distribution]:
    wheel = tmp_path / "brains_ai-1.3.1-py3-none-any.whl"
    members = {
        "brains/sample.py": b"VALUE = 1\n",
        "brains_ai-1.3.1.dist-info/METADATA": b"Name: brains-ai\nVersion: 1.3.1\n",
        "brains_ai-1.3.1.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "brains_ai-1.3.1.dist-info/entry_points.txt": (
            b"[console_scripts]\nbrains-ai = brains.cli.app:app\n"
        ),
        "brains_ai-1.3.1.dist-info/RECORD": b"",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    direct_url = json.dumps(
        {"url": wheel.resolve().as_uri(), "archive_info": {"hash": f"sha256={wheel_sha}"}},
        sort_keys=True,
    )
    prefix = tmp_path / "venv"
    interpreter = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = prefix / ("Scripts/brains-ai.exe" if os.name == "nt" else "bin/brains-ai")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"synthetic interpreter")
    executable.write_bytes(b"synthetic console launcher")
    installed = prefix / "site-packages"
    files: list[importlib.metadata.PackagePath] = []
    for name, content in members.items():
        if name.endswith("/RECORD"):
            continue
        target = installed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        item = importlib.metadata.PackagePath(name)
        encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        item.hash = importlib.metadata.FileHash(f"sha256={encoded.decode('ascii')}")
        item.size = len(content)
        files.append(item)
    direct_path = installed / "brains_ai-1.3.1.dist-info/direct_url.json"
    direct_path.write_text(direct_url, encoding="utf-8")
    direct_item = importlib.metadata.PackagePath("brains_ai-1.3.1.dist-info/direct_url.json")
    direct_bytes = direct_url.encode("utf-8")
    encoded = base64.urlsafe_b64encode(hashlib.sha256(direct_bytes).digest()).rstrip(b"=")
    direct_item.hash = importlib.metadata.FileHash(f"sha256={encoded.decode('ascii')}")
    direct_item.size = len(direct_bytes)
    files.append(direct_item)
    return wheel, prefix, executable, _Distribution(installed, direct_url, files)


def test_distribution_provenance_binds_wheel_payload_metadata_and_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, prefix, executable, distribution = _installed_fixture(tmp_path)
    interpreter = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: distribution)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(sys, "executable", str(interpreter))

    result = native_evidence.distribution_provenance(wheel, executable)

    assert result["wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert result["installed"]["console_entry_point"] == "brains.cli.app:app"
    assert len(result["installed"]["manifest_sha256"]) == 64
    (distribution.root / "brains/sample.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.distribution_provenance(wheel, executable)


def test_fresh_output_and_snapshot_contracts_are_content_free(tmp_path: Path) -> None:
    output = tmp_path / "evidence" / "result.json"
    native_evidence.require_fresh_output(output)
    output.write_text("stale", encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.require_fresh_output(output)

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text("secret-shaped synthetic content", encoding="utf-8")
    snapshot = native_evidence.snapshot_files((("adapter", config),))
    encoded = json.dumps(snapshot)
    assert "secret-shaped synthetic content" not in encoded
    assert (
        snapshot["adapter/settings.json"]["sha256"]
        == hashlib.sha256(b"secret-shaped synthetic content").hexdigest()
    )


def test_managed_backup_accounting_requires_exact_primary_and_known_states() -> None:
    baseline = {"codex/config.toml": {"size": 8, "sha256": "a" * 64}}
    wired = {"codex/config.toml": {"size": 9, "sha256": "b" * 64}}
    restored = {
        **baseline,
        "codex/config.toml.bak-20260903-010101": baseline["codex/config.toml"],
        "codex/config.toml.bak-20260903-010102": wired["codex/config.toml"],
    }
    assert len(native_evidence.account_managed_backups(baseline, wired, restored)) == 2
    changed = {**restored, "codex/config.toml": {"size": 7, "sha256": "c" * 64}}
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.account_managed_backups(baseline, wired, changed)
    unexpected = {**restored, "codex/unowned.txt": {"size": 1, "sha256": "a" * 64}}
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.account_managed_backups(baseline, wired, unexpected)


def test_explicit_runtime_tools_are_hashed_and_close_the_child_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / ("node.exe" if os.name == "nt" else "node")
    opencode = bin_dir / ("opencode.exe" if os.name == "nt" else "opencode")
    node.write_bytes(b"synthetic node")
    opencode.write_bytes(b"synthetic opencode")
    record, controlled = native_evidence.explicit_runtime_tools(
        json.dumps({"node": str(node.resolve()), "opencode": str(opencode.resolve())}),
        required=("node", "opencode"),
        prepend_paths=(tmp_path,),
    )
    assert record["node"]["sha256"] == hashlib.sha256(b"synthetic node").hexdigest()
    assert record["opencode"]["sha256"] == hashlib.sha256(b"synthetic opencode").hexdigest()
    assert controlled.split(os.pathsep) == [str(tmp_path.resolve()), str(bin_dir.resolve())]
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.explicit_runtime_tools(
            json.dumps({"node": str(node.resolve())}), required=("node", "opencode")
        )


def test_native_lifecycle_executes_ordered_transitions_and_exact_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", "disposable-native-service-host")
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(runtime))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(runtime / "state"))
    monkeypatch.setattr(native_lifecycle, "_boot_marker", lambda: "a" * 64)
    monkeypatch.setattr(native_lifecycle, "_kill_owned_tree", lambda _pid: None)

    config_path = home / ".codex/config.toml"
    baseline_content: bytes | None = None
    wired_content: bytes | None = None
    state = {"installed": False, "running": False}

    def fake_run(_executable: str, args: list[str], env: dict[str, str] | None = None) -> dict:
        nonlocal baseline_content, wired_content
        if args[0] == "setup":
            sessions = runtime / "state/sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (runtime / "state/admin-key").write_text("synthetic-key", encoding="utf-8")
            (sessions / "service.log").write_text("starting synthetic service\n", encoding="utf-8")
        elif args[0] == "wire":
            baseline_content = config_path.read_bytes()
            config_path.with_name("config.toml.bak-20260903-010101").write_bytes(baseline_content)
            config_path.write_bytes(baseline_content + b"# managed\n")
            wired_content = config_path.read_bytes()
        elif args[0] == "unwire":
            assert baseline_content is not None and wired_content is not None
            config_path.with_name("config.toml.bak-20260903-010102").write_bytes(wired_content)
            config_path.write_bytes(baseline_content)
        elif args[:2] == ["service", "install"]:
            state.update(installed=True, running=True)
        elif args[:2] == ["service", "stop"]:
            state["running"] = False
        elif args[:2] in (["service", "start"], ["service", "restart"]):
            state["running"] = True
        elif args[:2] == ["service", "uninstall"]:
            state.update(installed=False, running=False)
        return {"ok": True}

    pids = iter((101, 102, 103, 104, 105))

    def healthy(_executable: str, _label: str, timeout: float = 150) -> dict:
        pid = next(pids)
        marker = runtime / "state/sessions/service.pid"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"pid": pid, "start_time": pid * 10}), encoding="utf-8")
        return {
            "installed": True,
            "healthy": True,
            "runtime_classification": "installed-owned-ready",
            "service_pid": {"pid": pid, "confidence": "verified"},
            "listeners": {"gateway": True, "mcp": True},
            "mcp_protocol": {"ready": True},
        }

    def status(_executable: str, _label: str) -> dict:
        return {
            "installed": state["installed"],
            "healthy": False,
            "runtime_classification": "stopped",
            "service_pid": {"pid": None, "confidence": "absent"},
            "listeners": {"gateway": False, "mcp": False},
            "mcp_protocol": {"ready": False},
        }

    monkeypatch.setattr(native_lifecycle, "_run", fake_run)
    monkeypatch.setattr(native_lifecycle, "_wait_healthy", healthy)
    monkeypatch.setattr(native_lifecycle, "_status", status)
    monkeypatch.setattr(native_lifecycle, "_wait_removed", status)
    provenance = {"binding_sha256": "f" * 64}

    prepared = native_lifecycle.prepare("synthetic-brains-ai", "1" * 40, "codex", provenance)
    assert prepared["boundary"]["boot_changed"] is False
    monkeypatch.setattr(native_lifecycle, "_boot_marker", lambda: "b" * 64)
    verified = native_lifecycle.verify(
        "1" * 40,
        adapter="codex",
        login_observed=True,
        provenance=provenance,
    )
    assert [step["step"] for step in verified["steps"]] == [
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
    ]
    assert all(
        step["provenance_sha256"] == provenance["binding_sha256"] for step in verified["steps"]
    )
    assert verified["boundary"] == {
        "boot_changed": True,
        "login_transition_operator_attested": True,
    }
    assert verified["steps"][-1]["evidence"]["listeners_removed"] is True
    assert not runtime.exists()
    assert not config_path.exists()


def test_native_workflows_declare_full_matrix_and_success_only_upload() -> None:
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    native = (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    for workflow in (ci, native):
        assert "manager: task-scheduler" in workflow
        assert "manager: launchd" in workflow
        assert "manager: systemd-user" in workflow
        assert 'python: ["3.11", "3.12"]' in workflow
        assert "adapter: [copilot-cli, claude-code, codex, opencode]" in workflow
        assert "transport: [streamable-http]" in workflow
        assert "opencode-ai@1.18.25" in workflow
        assert "if: success()" in workflow
        assert "verify_native_evidence.py" in workflow
        assert "GITHUB_OUTPUT" not in workflow
    upload = native.split("- name: Upload sanitized native evidence", 1)[1]
    assert "if: always()" not in upload
    assert "if: success()" in upload


def test_evidence_verifier_recomputes_binding_and_rejects_step_tampering(
    tmp_path: Path,
) -> None:
    candidate = "1" * 40
    bound = {
        "source": {"commit": candidate, "tree": "2" * 40, "git_sha256": "3" * 64},
        "distribution": {
            "wheel": {
                "sha256": "4" * 64,
                "payload_manifest_sha256": "5" * 64,
            },
            "installed": {
                "manifest_sha256": "6" * 64,
                "metadata_sha256": "7" * 64,
                "direct_url_sha256": "8" * 64,
                "executable_sha256": "9" * 64,
                "interpreter_sha256": "a" * 64,
                "record_hashes_verified": 3,
            },
        },
        "runtime_tools": {},
    }
    binding = native_evidence.canonical_sha256(bound)
    provenance = {
        "schema": "brains.native-provenance.v1",
        "binding_sha256": binding,
        **bound,
    }
    steps = [
        {
            "sequence": index,
            "step": step,
            "passed": True,
            "provenance_sha256": binding,
            "evidence": {},
        }
        for index, step in enumerate(native_verifier.INSTALL_STEPS, start=1)
    ]
    record = {
        "schema": "brains.native-installation-evidence.v1",
        "passed": True,
        "matrix": {
            "manager": "task-scheduler",
            "python": "3.12",
            "adapter": "codex",
            "transport": "streamable-http",
        },
        "provenance": provenance,
        "steps": steps,
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    native_verifier.verify_record(
        path,
        kind="installation",
        candidate=candidate,
        manager="task-scheduler",
        python="3.12",
        adapter="codex",
    )
    record["steps"][0]["provenance_sha256"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(native_verifier.VerificationFailure):
        native_verifier.verify_record(
            path,
            kind="installation",
            candidate=candidate,
            manager="task-scheduler",
            python="3.12",
            adapter="codex",
        )


def test_installation_probe_refuses_to_overwrite_stale_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "evidence.json"
    output.write_text("stale-evidence", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/probe_native_installation.py"),
            "--candidate",
            "0" * 40,
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--git-executable",
            str(Path(shutil.which("git") or "")),
            "--tool",
            "codex",
            "--output",
            str(output),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1
    assert output.read_text(encoding="utf-8") == "stale-evidence"
    assert result.stdout == ""
    assert result.stderr == ""


def test_native_manager_probe_refuses_personal_state_before_provenance_or_manager(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    (home / ".brains").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    output = tmp_path / "guarded.json"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "BRAINS_NATIVE_EVIDENCE_DISPOSABLE": "disposable-native-service-host",
        "BRAINS_NATIVE_EVIDENCE_ROOT": str(runtime),
        "BRAINS_STATE_DIR": str(runtime / "state"),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/probe_native_service_lifecycle.py"),
            "prepare",
            "--candidate",
            "0" * 40,
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--git-executable",
            str(Path(shutil.which("git") or "")),
            "--adapter",
            "codex",
            "--output",
            str(output),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1
    assert json.loads(output.read_text(encoding="utf-8"))["error_type"] == "EvidenceFailure"
    assert not runtime.exists()
    assert result.stdout == ""
    assert result.stderr == ""
