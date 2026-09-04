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
import yaml

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
_INSTALLATION_PATH = Path(__file__).resolve().parents[1] / "scripts/probe_native_installation.py"
_INSTALLATION_SPEC = importlib.util.spec_from_file_location(
    "brains_test_native_installation", _INSTALLATION_PATH
)
assert _INSTALLATION_SPEC is not None and _INSTALLATION_SPEC.loader is not None
native_installation = importlib.util.module_from_spec(_INSTALLATION_SPEC)
_INSTALLATION_SPEC.loader.exec_module(native_installation)
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
    shadow_git = tmp_path / ("git-shadow.exe" if os.name == "nt" else "git-shadow")
    shutil.copy2(git, shadow_git)
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.source_provenance(repo, candidate, shadow_git)
    (repo / "untracked.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.source_provenance(repo, candidate, git)
    (repo / "untracked.txt").unlink()
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
    original_url = distribution._direct_url
    parsed_url = json.loads(original_url)
    parsed_url["url"] = parsed_url["url"].replace("file:///", "file://localhost/")
    distribution._direct_url = json.dumps(parsed_url)
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.distribution_provenance(wheel, executable)
    distribution._direct_url = original_url
    sample = next(item for item in distribution.files if str(item) == "brains/sample.py")
    recorded_hash = sample.hash
    sample.hash = None
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.distribution_provenance(wheel, executable)
    sample.hash = recorded_hash
    (distribution.root / "brains/sample.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.distribution_provenance(wheel, executable)


def test_package_manifest_binds_candidate_tree_and_exact_wheel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "source.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "source.txt")
    identity = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic Test",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Test",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
    }
    _git(repo, "commit", "-m", "synthetic package", env=identity)
    candidate = _git(repo, "rev-parse", "HEAD")
    wheel, _prefix, _executable, _distribution = _installed_fixture(tmp_path)
    git = Path(shutil.which("git") or "").resolve(strict=True)
    manifest = tmp_path / "native-wakeup-package-provenance.json"
    native_evidence.write_package_provenance(
        manifest,
        repo=repo,
        candidate=candidate,
        git_executable=git,
        wheel_path=wheel,
    )
    source = native_evidence.source_provenance(repo, candidate, git)
    bound = native_evidence.package_provenance(manifest, source=source, wheel_path=wheel)
    assert bound["candidate"] == candidate
    assert bound["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()

    stale = tmp_path / "stale-brains.whl"
    shutil.copy2(wheel, stale)
    with zipfile.ZipFile(stale, "a") as archive:
        archive.writestr("brains/stale.py", b"STALE = True\n")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.package_provenance(manifest, source=source, wheel_path=stale)

    regenerated = json.loads(manifest.read_text(encoding="utf-8"))
    regenerated["wheel_sha256"] = hashlib.sha256(stale.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(regenerated), encoding="utf-8")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.package_provenance(manifest, source=source, wheel_path=wheel)


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


def test_installation_definition_and_setup_evidence_rejects_false_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_installation.platform, "system", lambda: "Linux")
    command = [
        "/synthetic/python",
        "-m",
        "brains",
        "serve-all",
        "--gateway-host",
        "127.0.0.1",
        "--gateway-port",
        "24001",
        "--mcp-port",
        "24002",
    ]
    rendered = {
        "action": "would-install",
        "platform": "linux",
        "label": "brains-serve-all.service",
        "command": command,
        "endpoints": {
            "console": "http://127.0.0.1:24001/app",
            "mcp": "http://127.0.0.1:24002/mcp",
        },
        "unit": "WantedBy=default.target\nRestart=always\n",
    }
    evidence = native_installation._manager_definition_evidence(
        rendered, gateway_port=24001, mcp_port=24002
    )
    assert evidence["autostart"] is evidence["restart_on_failure"] is True
    rendered["unit"] = "WantedBy=default.target\nRestart=no\n"
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_installation._manager_definition_evidence(
            rendered, gateway_port=24001, mcp_port=24002
        )

    first = {
        "steps": [
            {
                "step": "init",
                "workspace": {"slug": "synthetic"},
                "admin_key": {"source": "generated"},
            }
        ]
    }
    second = {
        "steps": [
            {
                "step": "init",
                "workspace": {"slug": "synthetic"},
                "admin_key": {"source": "existing"},
            }
        ]
    }
    before = {"state/db": {"size": 1, "sha256": "a" * 64}}
    native_installation._assert_setup_idempotent(first, second, before, before)
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_installation._assert_setup_idempotent(
            first,
            second,
            before,
            {"state/db": {"size": 2, "sha256": "b" * 64}},
        )


def test_explicit_runtime_tools_are_hashed_and_close_the_child_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / ("node.exe" if os.name == "nt" else "node")
    opencode = bin_dir / ("opencode.exe" if os.name == "nt" else "opencode")
    node.write_bytes(b"synthetic node")
    opencode.write_bytes(b"synthetic opencode")
    node.chmod(0o755)
    opencode.chmod(0o755)
    monkeypatch.setattr(
        native_evidence.shutil,
        "which",
        lambda name, path: next(
            (
                str(candidate)
                for directory in path.split(os.pathsep)
                if (candidate := Path(directory) / name).is_file()
            ),
            None,
        ),
    )
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
    shadow = bin_dir / ("node-shadow.exe" if os.name == "nt" else "node-shadow")
    shadow.write_bytes(b"synthetic shadow")
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_evidence.explicit_runtime_tools(
            json.dumps({"node": str(shadow.resolve())}), required=("node",)
        )
    manager_bin = tmp_path / "manager-bin"
    manager_bin.mkdir()
    manager = manager_bin / ("powershell.exe" if os.name == "nt" else "powershell")
    collision = bin_dir / manager.name
    manager.write_bytes(b"synthetic manager")
    collision.write_bytes(b"shadow manager")
    manager.chmod(0o755)
    collision.chmod(0o755)
    with pytest.raises(native_evidence.ProvenanceFailure, match="resolution"):
        native_evidence.explicit_runtime_tools(
            json.dumps({"node": str(node.resolve()), "powershell": str(manager.resolve())}),
            required=("node", "powershell"),
        )


@pytest.mark.parametrize("reuse_pid", [False, True])
def test_native_lifecycle_executes_ordered_transitions_and_exact_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse_pid: bool
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

    pids = iter((101, 101, 103, 104, 105) if reuse_pid else (101, 102, 103, 104, 105))

    def healthy(_executable: str, _label: str, timeout: float = 150) -> dict:
        pid = next(pids)
        marker = runtime / "state/sessions/service.pid"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"pid": pid, "start_time": pid * 10}), encoding="utf-8")
        return {
            "platform": {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
                native_lifecycle.platform.system()
            ],
            "label": native_lifecycle.native_service_identity(
                {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
                    native_lifecycle.platform.system()
                ],
                _label,
            ),
            "state": "active",
            "installed": True,
            "healthy": True,
            "runtime_classification": "installed-owned-ready",
            "service_pid": {"pid": pid, "confidence": "verified"},
            "listeners": {"gateway": True, "mcp": True},
            "mcp_protocol": {"ready": True},
        }

    def status(_executable: str, _label: str) -> dict:
        return {
            "platform": {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
                native_lifecycle.platform.system()
            ],
            "label": native_lifecycle.native_service_identity(
                {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
                    native_lifecycle.platform.system()
                ],
                _label,
            ),
            "state": "inactive",
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

    if reuse_pid:
        with pytest.raises(native_lifecycle.EvidenceFailure, match="reused"):
            native_lifecycle.prepare("synthetic-brains-ai", "1" * 40, "codex", provenance)
        return
    executable = tmp_path / "synthetic-brains-ai"
    executable.write_bytes(b"synthetic executable")
    prepared = native_lifecycle.prepare(str(executable), "1" * 40, "codex", provenance)
    assert prepared["boundary"]["boot_changed"] is False
    prepared["passed"] = True
    prepare_path = tmp_path / "native-service-prepare.json"
    prepare_path.write_text(json.dumps(prepared), encoding="utf-8")
    prepare_sha256 = hashlib.sha256(prepare_path.read_bytes()).hexdigest()
    original_plan = json.loads(native_lifecycle._plan_path().read_text(encoding="utf-8"))
    for field, value in (
        ("boot_marker", "c" * 64),
        ("executable", str(tmp_path / "substituted-brains-ai")),
    ):
        tampered = json.loads(json.dumps(original_plan))
        if field == "executable":
            Path(value).write_bytes(b"substituted")
        tampered[field] = value
        tampered["plan_core_sha256"] = native_evidence.canonical_sha256(
            {key: tampered[key] for key in native_lifecycle.PLAN_CORE_FIELDS}
        )
        native_lifecycle._plan_path().write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(native_lifecycle.EvidenceFailure):
            native_lifecycle.verify(
                "1" * 40,
                adapter="codex",
                provenance=provenance,
                prepare_record_path=prepare_path,
                prepare_record_sha256=prepare_sha256,
                installed_executable=executable,
            )
    native_lifecycle._plan_path().write_text(json.dumps(original_plan), encoding="utf-8")
    with pytest.raises(native_lifecycle.EvidenceFailure, match="machine-observed reboot"):
        native_lifecycle.verify(
            "1" * 40,
            adapter="codex",
            provenance=provenance,
            prepare_record_path=prepare_path,
            prepare_record_sha256=prepare_sha256,
            installed_executable=executable,
        )
    monkeypatch.setattr(native_lifecycle, "_boot_marker", lambda: "b" * 64)
    verified = native_lifecycle.verify(
        "1" * 40,
        adapter="codex",
        provenance=provenance,
        prepare_record_path=prepare_path,
        prepare_record_sha256=prepare_sha256,
        installed_executable=executable,
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
        "prepared_boot_marker_sha256": "a" * 64,
        "observed_boot_marker_sha256": "b" * 64,
        "login_transition_attestation": None,
    }
    assert verified["steps"][-1]["evidence"]["listeners_removed"] is True
    cleanup = native_lifecycle.cleanup(
        expected_executable=executable,
        completed_restoration=next(
            step["evidence"]
            for step in verified["steps"]
            if step["step"] == "configuration-restored"
        ),
    )
    assert cleanup["runtime_root_removed"] is True
    assert not runtime.exists()
    assert not config_path.exists()


def test_native_readiness_wait_rejects_partial_listener_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
        native_lifecycle.platform.system()
    ]
    partial = {
        "platform": platform_slug,
        "label": native_lifecycle.native_service_identity(platform_slug, "brains-serve-all-test"),
        "state": "active",
        "installed": True,
        "healthy": True,
        "listeners": {"gateway": True, "mcp": False},
        "mcp_protocol": {"ready": False},
    }
    ready = {
        **partial,
        "listeners": {"gateway": True, "mcp": True},
        "mcp_protocol": {"ready": True},
    }
    reports = iter((partial, ready))
    monkeypatch.setattr(native_lifecycle, "_status", lambda *_args: next(reports))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _seconds: None)
    assert native_lifecycle._wait_healthy("synthetic", "brains-serve-all-test", timeout=1) is ready


def test_native_lifecycle_rejects_partial_readiness_and_noop_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[
        native_lifecycle.platform.system()
    ]
    partial = {
        "platform": platform_slug,
        "label": native_lifecycle.native_service_identity(platform_slug, "brains-serve-all-test"),
        "state": "active",
        "installed": True,
        "healthy": True,
        "service_pid": {"pid": 101, "confidence": "verified"},
        "listeners": {"gateway": True, "mcp": False},
        "mcp_protocol": {"ready": False},
    }
    ready = {
        **partial,
        "listeners": {"gateway": True, "mcp": True},
        "mcp_protocol": {"ready": True},
    }
    reports = iter((partial, ready))
    monkeypatch.setattr(native_lifecycle, "_status", lambda *_args: next(reports))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _seconds: None)
    assert native_lifecycle._wait_healthy("synthetic", "brains-serve-all-test", timeout=1) == ready
    with pytest.raises(native_lifecycle.EvidenceFailure, match="stopped"):
        native_lifecycle._assert_stopped(
            {
                "installed": True,
                "healthy": True,
                "listeners": {"gateway": True, "mcp": True},
                "mcp_protocol_ready": True,
                "owned_process": {
                    "pid": 101,
                    "confidence": "verified",
                    "start_marker_sha256": "a" * 64,
                },
            }
        )


@pytest.mark.parametrize("report", [{}, {"platform": "linux", "label": "wrong", "state": "x"}])
def test_native_wait_removed_rejects_empty_or_partial_status(
    monkeypatch: pytest.MonkeyPatch, report: dict
) -> None:
    monkeypatch.setattr(native_lifecycle, "_status", lambda *_args: report)
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(native_lifecycle.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _seconds: None)
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._wait_removed("synthetic", "brains-serve-all-test", timeout=1)


def test_native_cleanup_rejects_stale_plan_and_mismatched_adapter() -> None:
    journey = native_lifecycle._journey("1" * 40, "codex", "f" * 64)
    assert native_lifecycle._valid_journey(
        journey, candidate="1" * 40, adapter="codex", provenance_sha256="f" * 64
    )
    assert not native_lifecycle._valid_journey(
        journey, candidate="1" * 40, adapter="claude-code", provenance_sha256="f" * 64
    )
    stale = json.loads(json.dumps(journey))
    stale["journey_id"] = "e" * 64
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._prepared_binding_matched(
            {"journey": stale},
            journey,
            "2" * 64,
            candidate="1" * 40,
            adapter="codex",
            provenance_sha256="f" * 64,
        )


@pytest.mark.parametrize("field", ["label", "adapter", "executable", "baseline_snapshot"])
def test_native_cleanup_rejects_tampered_operational_plan(field: str) -> None:
    journey = native_lifecycle._journey("1" * 40, "codex", "f" * 64)
    plan = {
        "candidate": "1" * 40,
        "adapter": "codex",
        "provenance": {"binding_sha256": "f" * 64},
        "journey": journey,
        "executable": "synthetic-brains-ai",
        "label": "brains-serve-all-evidence-11111111",
        "gateway_port": 24001,
        "mcp_port": 24002,
        "boot_marker": "a" * 64,
        "original_snapshot": {},
        "baseline_snapshot": {"config": {"size": 1, "sha256": "b" * 64}},
        "wired_snapshot": {"config": {"size": 2, "sha256": "c" * 64}},
        "steps": [],
    }
    plan["plan_core_sha256"] = native_evidence.canonical_sha256(
        {key: plan[key] for key in native_lifecycle.PLAN_CORE_FIELDS}
    )
    tampered = json.loads(json.dumps(plan))
    tampered[field] = "tampered" if field != "baseline_snapshot" else {}
    with pytest.raises(native_lifecycle.EvidenceFailure, match="plan digest"):
        native_lifecycle._prepared_binding_matched(
            tampered,
            journey,
            plan["plan_core_sha256"],
            candidate="1" * 40,
            adapter="codex",
            provenance_sha256="f" * 64,
        )


def test_native_workflows_declare_full_matrix_and_success_only_upload() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = (
        ("ci.yml", "native-installation-probe"),
        ("native-service-evidence.yml", "manager-cycle"),
    )
    for filename, job_name in cases:
        workflow = yaml.safe_load(
            (root / ".github/workflows" / filename).read_text(encoding="utf-8")
        )
        job = workflow["jobs"][job_name]
        matrix = job["strategy"]["matrix"]
        assert matrix == {
            "host": [
                {"os": "windows-2022", "manager": "task-scheduler"},
                {"os": "macos-14", "manager": "launchd"},
                {"os": "ubuntu-24.04", "manager": "systemd-user"},
            ],
            "python": ["3.11", "3.12"],
            "adapter": ["copilot-cli", "claude-code", "codex", "opencode"],
            "transport": ["streamable-http"],
        }
        steps = job["steps"]
        provision = next(
            step for step in steps if step.get("name") == "Provision pinned supported OpenCode"
        )
        assert provision["run"] == "npm install --global opencode-ai@1.18.25"
        verifier = next(
            step for step in steps if "verify_native_evidence.py" in str(step.get("run", ""))
        )
        probe = next(
            step
            for step in steps
            if "probe_native_" in str(step.get("run", ""))
            and "verify_native_evidence.py" not in str(step.get("run", ""))
        )
        upload = next(
            step for step in steps if str(step.get("name", "")).startswith("Upload sanitized")
        )
        assert verifier["run"]
        assert "--package-manifest" in probe["run"]
        assert upload["if"] == "success()"
        assert upload["with"]["if-no-files-found"] == "error"
        if filename == "ci.yml":
            environment = next(step for step in steps if step.get("id") == "native-environment")
            assert 'echo "python=$probe_python" >> "$GITHUB_OUTPUT"' in environment["run"]
            assert "steps.native-environment.outputs.python" in str(steps)
        package = workflow["jobs"]["package"]
        prepare_steps = [
            step for step in package["steps"] if "--prepare-package" in str(step.get("run", ""))
        ]
        assert len(prepare_steps) == 1
        assert package["outputs"]["manifest-sha256"]


def _provenance(candidate: str, *, service: bool = False) -> dict:
    tools = (
        {
            "powershell": {"executable": "powershell.exe", "sha256": "b" * 64},
            "schtasks": {"executable": "schtasks.exe", "sha256": "c" * 64},
            "taskkill": {"executable": "taskkill.exe", "sha256": "d" * 64},
        }
        if service
        else {}
    )
    bound = {
        "source": {"commit": candidate, "tree": "2" * 40, "git_sha256": "3" * 64},
        "package": {
            "schema": "brains-native-wakeup-package-provenance/v1",
            "candidate": candidate,
            "source_tree": "2" * 40,
            "wheel_filename": "brains_ai-1.3.1-py3-none-any.whl",
            "wheel_sha256": "4" * 64,
            "wheel_record_sha256": "5" * 64,
            "wheel_archive_metadata_sha256": "6" * 64,
            "wheel_archive_wheel_sha256": "7" * 64,
            "builder_git_sha256": "8" * 64,
            "manifest_sha256": "9" * 64,
        },
        "distribution": {
            "wheel": {"sha256": "4" * 64, "size": 42, "payload_manifest_sha256": "5" * 64},
            "installed": {
                "name": "brains-ai",
                "version": "1.3.1",
                "manifest_sha256": "6" * 64,
                "metadata_sha256": "7" * 64,
                "direct_url_sha256": "8" * 64,
                "executable_sha256": "9" * 64,
                "interpreter_sha256": "a" * 64,
                "record_hashes_verified": 3,
                "console_entry_point": "brains.cli.app:app",
            },
        },
        "runtime_tools": tools,
    }
    return {
        "schema": "brains.native-provenance.v1",
        "binding_sha256": native_evidence.canonical_sha256(bound),
        **bound,
    }


def _installation_record(candidate: str) -> dict:
    provenance = _provenance(candidate)
    binding = provenance["binding_sha256"]
    evidence = {
        "provenance": {
            "candidate_bound": True,
            "wheel_bound": True,
            "installed_distribution_bound": True,
            "executable_bound": True,
        },
        "harness": {"adapter": "codex", "binary_required_for_wire": False},
        "manager-definition": {
            "manager": "task-scheduler",
            "platform": "windows",
            "native_execution": False,
            "identity": "BrainsServeAll",
            "gateway_port": 24679,
            "mcp_port": 24680,
            "command_sha256": "a" * 64,
            "definition_sha256": "b" * 64,
            "autostart": True,
            "restart_on_failure": True,
        },
        "wire": {
            "adapter": "codex",
            "protocol": "streamable-http",
            "endpoint": {"host": "loopback", "port": 24680, "path": "/mcp"},
            "baseline_config_sha256": "b" * 64,
            "wired_config_sha256": "c" * 64,
        },
        "restoration": {
            "baseline_config_sha256": "b" * 64,
            "restored_config_sha256": "d" * 64,
            "primary_configuration_restored": True,
            "managed_backup_count": 1,
            "managed_backup_manifest_sha256": "e" * 64,
            "initial_home_restored": True,
            "setup_idempotent": True,
            "setup_state_sha256": "f" * 64,
        },
    }
    steps = [
        {
            "sequence": index,
            "step": step,
            "passed": True,
            "provenance_sha256": binding,
            "evidence": evidence[step],
        }
        for index, step in enumerate(native_verifier.INSTALL_STEPS, start=1)
    ]
    return {
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


@pytest.mark.parametrize(
    "mutation",
    ["empty-evidence", "false-claim", "bad-tree", "missing-hash", "tool-omission", "no-op-wire"],
)
def test_evidence_verifier_executable_rejects_mutated_claims(tmp_path: Path, mutation: str) -> None:
    candidate = "1" * 40
    record = _installation_record(candidate)
    if mutation == "empty-evidence":
        record["steps"][0]["evidence"] = {}
    elif mutation == "false-claim":
        record["steps"][0]["evidence"]["wheel_bound"] = False
    elif mutation == "bad-tree":
        record["provenance"]["source"]["tree"] = "2" * 64
    elif mutation == "missing-hash":
        del record["provenance"]["distribution"]["installed"]["executable_sha256"]
    elif mutation == "tool-omission":
        record["provenance"]["runtime_tools"] = {"node": {"executable": "node", "sha256": "f" * 64}}
    else:
        record["steps"][3]["evidence"]["wired_config_sha256"] = "b" * 64
    bound = {
        key: record["provenance"][key]
        for key in ("source", "package", "distribution", "runtime_tools")
    }
    record["provenance"]["binding_sha256"] = native_evidence.canonical_sha256(bound)
    for step in record["steps"]:
        step["provenance_sha256"] = record["provenance"]["binding_sha256"]
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(_VERIFIER_PATH),
            "--kind",
            "installation",
            "--candidate",
            candidate,
            "--manager",
            "task-scheduler",
            "--python",
            "3.12",
            "--adapter",
            "codex",
            "--input",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == ""


def test_evidence_verifier_executable_accepts_complete_installation(tmp_path: Path) -> None:
    candidate = "1" * 40
    record = _installation_record(candidate)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(_VERIFIER_PATH),
            "--kind",
            "installation",
            "--candidate",
            candidate,
            "--manager",
            "task-scheduler",
            "--python",
            "3.12",
            "--adapter",
            "codex",
            "--input",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0


def _service_record(candidate: str) -> dict:
    provenance = _provenance(candidate, service=True)
    binding = provenance["binding_sha256"]
    matrix = {
        "manager": "task-scheduler",
        "python": "3.12",
        "adapter": "codex",
        "transport": "streamable-http",
    }
    journey_bound = {
        "candidate": candidate,
        **matrix,
        "provenance_sha256": binding,
        "journey_id": "0" * 64,
    }
    journey = {
        "schema": "brains.native-service-journey.v1",
        **journey_bound,
        "binding_sha256": native_evidence.canonical_sha256(journey_bound),
    }
    logical_label = "brains-serve-all-evidence-11111111"
    native_label = native_lifecycle.native_service_identity("windows", logical_label)

    def active(pid: int, marker: str) -> dict:
        return {
            "manager": "task-scheduler",
            "platform": "windows",
            "label": native_label,
            "state": "active",
            "installed": True,
            "healthy": True,
            "runtime_classification": "installed-owned-ready",
            "owned_process": {
                "pid": pid,
                "confidence": "verified",
                "start_marker_sha256": marker * 64,
            },
            "listeners": {"gateway": True, "mcp": True},
            "mcp_protocol_ready": True,
        }

    inactive = {
        "manager": "task-scheduler",
        "platform": "windows",
        "label": native_label,
        "state": "inactive",
        "installed": True,
        "healthy": False,
        "runtime_classification": "stopped",
        "owned_process": {"pid": None, "confidence": "absent", "start_marker_sha256": None},
        "listeners": {"gateway": False, "mcp": False},
        "mcp_protocol_ready": False,
    }
    evidence = {
        "provenance": {
            "candidate_bound": True,
            "wheel_bound": True,
            "installed_distribution_bound": True,
            "executable_bound": True,
        },
        "manager-identity": {
            "manager": "task-scheduler",
            "label": logical_label,
            "platform": "Windows",
        },
        "endpoint-contract": {
            "host": "loopback",
            "gateway_port": 24001,
            "mcp_port": 24002,
            "mcp_path": "/mcp",
            "transport": "streamable-http",
        },
        "adapter-wired": {
            "adapter": "codex",
            "transport": "streamable-http",
            "baseline_config_sha256": "b" * 64,
            "wired_config_sha256": "c" * 64,
        },
        "installed": active(101, "1"),
        "stopped": inactive,
        "started": active(102, "2"),
        "restarted": active(103, "3"),
        "manager-recovered-owned-process": active(104, "4"),
        "boundary-prepared": {
            "boot_marker_sha256": "5" * 64,
            "login_transition_attestation": None,
        },
        "boundary-verified": {
            **active(105, "6"),
            "boot_changed": True,
            "prepared_boot_marker_sha256": "5" * 64,
            "observed_boot_marker_sha256": "6" * 64,
            "login_transition_attestation": None,
        },
        "configuration-restored": {
            "baseline_config_sha256": "b" * 64,
            "restored_config_sha256": "d" * 64,
            "primary_configuration_restored": True,
            "managed_backup_count": 1,
            "managed_backup_manifest_sha256": "e" * 64,
        },
        "teardown": {
            **inactive,
            "installed": False,
            "definition_removed": True,
            "listeners_removed": True,
            "initial_client_home_restored": True,
            "service_log_sha256": "f" * 64,
            "service_log_line_count": 1,
        },
    }
    steps = [
        {
            "sequence": index,
            "step": name,
            "passed": True,
            "provenance_sha256": binding,
            "evidence": evidence[name],
        }
        for index, name in enumerate(native_verifier.SERVICE_STEPS, start=1)
    ]
    return {
        "schema": "brains.native-service-evidence.v1",
        "phase": "verify",
        "passed": True,
        "matrix": matrix,
        "provenance": provenance,
        "journey": journey,
        "plan_core_sha256": "2" * 64,
        "prepare_record_sha256": "0" * 64,
        "steps": steps,
        "boundary": {
            "boot_changed": True,
            "prepared_boot_marker_sha256": "5" * 64,
            "observed_boot_marker_sha256": "6" * 64,
            "login_transition_attestation": None,
        },
    }


def _prepare_record(verified: dict) -> dict:
    prepared = json.loads(json.dumps(verified))
    prepared["phase"] = "prepare"
    prepared.pop("prepare_record_sha256")
    prepared["steps"] = prepared["steps"][: len(native_verifier.SERVICE_PREPARE_STEPS)]
    prepared["boundary"] = {"boot_changed": False, "login_transition_attestation": None}
    return prepared


def _cleanup_record(normal: dict, prior: Path, prepare: Path, *, prepared: bool) -> dict:
    inactive = json.loads(json.dumps(normal["steps"][5]["evidence"]))
    inactive["installed"] = False
    return {
        "schema": "brains.native-service-evidence.v1",
        "phase": "cleanup",
        "passed": True,
        "matrix": normal["matrix"],
        "provenance": normal["provenance"],
        "journey": normal["journey"],
        "plan_core_sha256": normal["plan_core_sha256"],
        "cleanup": {
            "final_status": inactive,
            "baseline_config_sha256": "b" * 64,
            "restored_config_sha256": "b" * 64,
            "primary_configuration_restored": True,
            "managed_backup_count": 1,
            "managed_backup_manifest_sha256": "e" * 64,
            "initial_client_home_restored": True,
            "definition_removed": True,
            "listeners_removed": True,
            "runtime_root_removed": True,
            "prepared_binding_matched": prepared,
            "prior_normal_record_sha256": hashlib.sha256(prior.read_bytes()).hexdigest(),
            "prepare_record_sha256": hashlib.sha256(prepare.read_bytes()).hexdigest(),
        },
    }


def _run_service_verifier(candidate: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    inputs = [item for path in paths for item in ("--input", str(path))]
    return subprocess.run(
        [
            sys.executable,
            str(_VERIFIER_PATH),
            "--kind",
            "service",
            "--candidate",
            candidate,
            "--manager",
            "task-scheduler",
            "--python",
            "3.12",
            "--adapter",
            "codex",
            *inputs,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "partial-readiness",
        "false-health",
        "false-gateway",
        "false-mcp-protocol",
        "wrong-installed-label",
        "wrong-stopped-label",
        "wrong-started-label",
        "wrong-restarted-label",
        "wrong-recovery-label",
        "wrong-boundary-label",
        "wrong-teardown-label",
        "same-pid",
        "missing-tool",
        "false-cleanup-link",
        "false-boundary",
        "bare-attestation",
        "forged-attestation",
        "swapped-record",
        "bad-classification",
        "equal-marker",
        "missing-marker",
        "stale-package-wheel",
        "regenerated-package-manifest",
        "cleanup-classification",
    ],
)
def test_service_verifier_rejects_incomplete_or_unlinked_evidence(
    tmp_path: Path, mutation: str
) -> None:
    candidate = "1" * 40
    normal = _service_record(candidate)
    prepare_path = tmp_path / "native-service-prepare.json"
    normal_path = tmp_path / "native-service-evidence.json"
    cleanup_path = tmp_path / "native-service-cleanup.json"
    if mutation == "partial-readiness":
        normal["steps"][4]["evidence"]["listeners"]["mcp"] = False
    elif mutation == "false-health":
        normal["steps"][4]["evidence"]["healthy"] = False
    elif mutation == "false-gateway":
        normal["steps"][4]["evidence"]["listeners"]["gateway"] = False
    elif mutation == "false-mcp-protocol":
        normal["steps"][4]["evidence"]["mcp_protocol_ready"] = False
    elif mutation.startswith("wrong-") and mutation.endswith("-label"):
        step = {
            "wrong-installed-label": "installed",
            "wrong-stopped-label": "stopped",
            "wrong-started-label": "started",
            "wrong-restarted-label": "restarted",
            "wrong-recovery-label": "manager-recovered-owned-process",
            "wrong-boundary-label": "boundary-verified",
            "wrong-teardown-label": "teardown",
        }[mutation]
        next(row for row in normal["steps"] if row["step"] == step)["evidence"]["label"] = (
            "brains-serve-all-evidence-wrong"
        )
    elif mutation == "same-pid":
        normal["steps"][6]["evidence"]["owned_process"]["pid"] = 101
    elif mutation == "missing-tool":
        del normal["provenance"]["runtime_tools"]["taskkill"]
        bound = {
            key: normal["provenance"][key]
            for key in ("source", "package", "distribution", "runtime_tools")
        }
        normal["provenance"]["binding_sha256"] = native_evidence.canonical_sha256(bound)
        for step in normal["steps"]:
            step["provenance_sha256"] = normal["provenance"]["binding_sha256"]
    elif mutation in {"false-boundary", "bare-attestation", "forged-attestation"}:
        boundary_step = next(
            step for step in normal["steps"] if step["step"] == "boundary-verified"
        )["evidence"]
        if mutation == "false-boundary":
            boundary_step["boot_changed"] = False
            normal["boundary"]["boot_changed"] = False
        elif mutation == "bare-attestation":
            boundary_step["login_transition_attestation"] = True
            normal["boundary"]["login_transition_attestation"] = True
        else:
            forged = {
                "schema": "brains.native-login-attestation.v1",
                "provenance_sha256": normal["provenance"]["binding_sha256"],
                "operator_attested": True,
            }
            forged["statement_sha256"] = native_evidence.canonical_sha256(forged)
            boundary_step["login_transition_attestation"] = forged
            normal["boundary"]["login_transition_attestation"] = forged
    elif mutation == "bad-classification":
        normal["steps"][4]["evidence"]["runtime_classification"] = "stopped"
    elif mutation == "stale-package-wheel":
        normal["provenance"]["package"]["wheel_sha256"] = "a" * 64
    elif mutation == "regenerated-package-manifest":
        normal["provenance"]["package"]["manifest_sha256"] = "a" * 64
    elif mutation in {"equal-marker", "missing-marker"}:
        boundary_step = next(
            step for step in normal["steps"] if step["step"] == "boundary-verified"
        )["evidence"]
        if mutation == "equal-marker":
            boundary_step["observed_boot_marker_sha256"] = boundary_step[
                "prepared_boot_marker_sha256"
            ]
            normal["boundary"]["observed_boot_marker_sha256"] = normal["boundary"][
                "prepared_boot_marker_sha256"
            ]
        else:
            del boundary_step["observed_boot_marker_sha256"]
            del normal["boundary"]["observed_boot_marker_sha256"]
    prepared = _prepare_record(normal)
    prepare_path.write_text(json.dumps(prepared), encoding="utf-8")
    normal["prepare_record_sha256"] = hashlib.sha256(prepare_path.read_bytes()).hexdigest()
    normal_path.write_text(json.dumps(normal), encoding="utf-8")
    cleanup = _cleanup_record(normal, normal_path, prepare_path, prepared=False)
    if mutation == "false-cleanup-link":
        cleanup["cleanup"]["prior_normal_record_sha256"] = "0" * 64
    elif mutation == "cleanup-classification":
        cleanup["cleanup"]["final_status"]["runtime_classification"] = "absent"
    if mutation == "swapped-record":
        cleanup["journey"] = json.loads(json.dumps(normal["journey"]))
        cleanup["journey"]["journey_id"] = "1" * 64
        journey_bound = {
            key: cleanup["journey"][key]
            for key in cleanup["journey"]
            if key not in {"schema", "binding_sha256"}
        }
        cleanup["journey"]["binding_sha256"] = native_evidence.canonical_sha256(journey_bound)
    cleanup_path.write_text(json.dumps(cleanup), encoding="utf-8")
    completed = _run_service_verifier(candidate, prepare_path, normal_path, cleanup_path)
    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == ""


def test_service_verifier_accepts_cryptographically_linked_cleanup(tmp_path: Path) -> None:
    candidate = "1" * 40
    normal = _service_record(candidate)
    prepare_path = tmp_path / "native-service-prepare.json"
    normal_path = tmp_path / "native-service-evidence.json"
    cleanup_path = tmp_path / "native-service-cleanup.json"
    prepared = _prepare_record(normal)
    prepare_path.write_text(json.dumps(prepared), encoding="utf-8")
    normal["prepare_record_sha256"] = hashlib.sha256(prepare_path.read_bytes()).hexdigest()
    normal_path.write_text(json.dumps(normal), encoding="utf-8")
    cleanup = _cleanup_record(normal, normal_path, prepare_path, prepared=False)
    cleanup_path.write_text(json.dumps(cleanup), encoding="utf-8")
    completed = _run_service_verifier(candidate, prepare_path, normal_path, cleanup_path)
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""


def test_service_verifier_accepts_bound_prepare_cycle_cleanup(tmp_path: Path) -> None:
    candidate = "1" * 40
    normal = _prepare_record(_service_record(candidate))
    normal_path = tmp_path / "native-service-evidence.json"
    cleanup_path = tmp_path / "native-service-cleanup.json"
    normal_path.write_text(json.dumps(normal), encoding="utf-8")
    cleanup = _cleanup_record(normal, normal_path, normal_path, prepared=True)
    cleanup_path.write_text(json.dumps(cleanup), encoding="utf-8")
    completed = _run_service_verifier(candidate, normal_path, cleanup_path)
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""


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
            "--package-manifest",
            str(tmp_path / "missing-manifest.json"),
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
            "--package-manifest",
            str(tmp_path / "missing-manifest.json"),
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
