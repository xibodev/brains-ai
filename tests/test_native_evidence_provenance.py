from __future__ import annotations

import ast
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest
import yaml

from brains.service import linux, macos, windows
from brains.service.common import ServiceSpec

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
    # Build the command through ServiceSpec rather than by hand: the probe reads
    # the rendered display string, and a hand-written fixture is free to disagree
    # with what the renderer actually emits.
    command = ServiceSpec(
        program="/synthetic/python",
        args=[
            "-m",
            "brains",
            "serve-all",
            "--gateway-host",
            "127.0.0.1",
            "--gateway-port",
            "24001",
            "--mcp-port",
            "24002",
        ],
        gateway_port=24001,
        mcp_port=24002,
    ).command_line
    assert isinstance(command, str)
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

    # A service interpreter can live under a path containing a space, which the
    # renderer quotes. That quoting must not be read as part of the arguments.
    # The path stays POSIX so the check runs on the host executing this test.
    quoted = dict(rendered)
    quoted["command"] = ServiceSpec(
        program="/opt/brains runtime/python",
        args=[
            "-m",
            "brains",
            "serve-all",
            "--gateway-host",
            "127.0.0.1",
            "--gateway-port",
            "24001",
            "--mcp-port",
            "24002",
        ],
        gateway_port=24001,
        mcp_port=24002,
    ).command_line
    assert native_installation._manager_definition_evidence(
        quoted, gateway_port=24001, mcp_port=24002
    )["autostart"]

    # A truncated argument list must still be rejected.
    truncated = dict(rendered)
    truncated["command"] = "/synthetic/python -m brains serve-all --gateway-host 127.0.0.1"
    with pytest.raises(native_evidence.ProvenanceFailure):
        native_installation._manager_definition_evidence(
            truncated, gateway_port=24001, mcp_port=24002
        )

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


def _mock_native_response(system: str, identity: str, expected: dict, registered: bool) -> str:
    if system == "Windows":
        xml = None
        if registered:
            task = ET.fromstring(expected["content"])
            ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
            principal = task.find(ns + "Principals/" + ns + "Principal")
            actions = task.find(ns + "Actions")
            trigger = task.find(ns + "Triggers/" + ns + "LogonTrigger")
            assert principal is not None and actions is not None and trigger is not None
            principal.set("id", "NormalizedPrincipal")
            actions.set("Context", "NormalizedPrincipal")
            trigger.set("id", "NormalizedTrigger")
            enabled = trigger.find(ns + "Enabled")
            if enabled is not None:
                trigger.remove(enabled)  # Schema default is true.
            ET.SubElement(trigger, ns + "ExecutionTimeLimit").text = "PT72H"
            actions[0][:] = list(reversed(list(actions[0])))
            for field in ("Command", "WorkingDirectory"):
                node = actions[0].find(ns + field)
                if node is not None and node.text:
                    node.text = node.text.replace("/", "\\").upper()
            xml = ET.tostring(task, encoding="unicode")
        return json.dumps({"xml": xml, "principal_matches": True, "trigger_matches": True})
    if system == "Linux":
        unit = (
            dict(
                line.split("=", 1)
                for line in expected["content"].splitlines()
                if "=" in line and not line.startswith("#")
            )
            if expected
            else {}
        )
        command = unit.get("ExecStart", "")
        return "\n".join(
            f"{key}={value}"
            for key, value in {
                "Id": identity,
                "LoadState": "loaded" if registered else "not-found",
                "ActiveState": "active" if registered else "inactive",
                "FragmentPath": expected["definition_path"] if registered else "",
                "ExecStart": (
                    f"{{ path={shlex.split(command)[0]} ; argv[]={command} ; "
                    "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
                    "code=(null) ; status=0/0 }"
                )
                if registered
                else "",
                "Environment": unit.get("Environment", "") if registered else "",
                "WorkingDirectory": unit.get("WorkingDirectory", "") if registered else "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
            }.items()
            if key != "ExecStart" or registered
        )
    if not registered:
        return ""
    plist = plistlib.loads(expected["content"].encode())
    args = "\n".join(f"\t\t{arg}" for arg in plist["ProgramArguments"])
    return (
        f"gui/1000/{identity} = {{\n\tpath = {expected['definition_path']}\n"
        f"\tprogram = {plist['ProgramArguments'][0]}\n\targuments = {{\n{args}\n\t}}\n"
        f"\tworking directory = {plist['WorkingDirectory']}\n\tenvironment = {{\n"
        f"\t\tBRAINS_STATE_DIR => {plist['EnvironmentVariables']['BRAINS_STATE_DIR']}\n\t}}\n}}\n"
    )


def _lifecycle_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    executable: Path,
    provenance: dict | Exception,
    *extra: str,
) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "prefix", str(executable.parent.parent))
    monkeypatch.setattr(
        native_lifecycle,
        "create_provenance",
        Mock(side_effect=provenance)
        if isinstance(provenance, Exception)
        else lambda **_kw: provenance,
    )
    monkeypatch.setattr(native_lifecycle, "explicit_runtime_tools", lambda *_args, **_kw: ({}, ""))
    monkeypatch.setattr(native_lifecycle.getpass, "getuser", lambda: "synthetic-operator")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_native_service_lifecycle.py",
            phase,
            "--candidate",
            "1" * 40,
            "--wheel",
            str(tmp_path / "synthetic.whl"),
            "--package-manifest",
            str(tmp_path / "synthetic-manifest.json"),
            "--git-executable",
            str(tmp_path / "synthetic-git"),
            "--adapter",
            "codex",
            "--output",
            str(output),
            *extra,
        ],
    )
    code = native_lifecycle.main()
    return code, json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "reuse-pid",
        "prepare-partial-install",
        "prepare-partial-definition",
        "manager-cycle-partial-install",
        "config-drift",
        "native-drift",
        "windows-policy-drift",
        "windows-policy-foreign-principal",
        "verify-mid-step",
        "verify-restoration",
        "cleanup-config-drift",
        "manager-cycle-cleanup",
        "runtime-drift",
        "runtime-marker-drift",
        "runtime-directory-drift",
        "runtime-link-drift",
    ],
)
def test_native_lifecycle_executes_ordered_transitions_and_exact_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    system: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if scenario.startswith("windows-policy-") and system != "Windows":
        pytest.skip("Windows registered policy regression")
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: system)
    monkeypatch.setattr(native_lifecycle.os, "getuid", lambda: 1000, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", "disposable-native-service-host")
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(runtime))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(runtime / "state"))
    monkeypatch.setattr(native_lifecycle, "_boot_marker", lambda: "a" * 64)
    monkeypatch.setattr(native_lifecycle, "_kill_owned_tree", lambda _pid: None)
    ports = iter((24001, 24002))
    monkeypatch.setattr(native_lifecycle, "_port", lambda: next(ports))

    config_path = home / ".codex/config.toml"
    baseline_content: bytes | None = None
    wired_content: bytes | None = None
    state = {"installed": False, "running": False}
    expected_definition: dict = {}
    original_expected = native_lifecycle._expected_native_definition

    def expected(plan: dict) -> dict:
        expected_definition.update(original_expected(plan))
        return dict(expected_definition)

    def native_command(args: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
        slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[system]
        identity = native_lifecycle.native_service_identity(
            slug, "brains-serve-all-evidence-11111111"
        )
        registered = state["installed"] and (system != "Darwin" or state["running"])
        content = _mock_native_response(system, identity, expected_definition, registered)
        if scenario == "native-drift" and registered:
            content = content.replace("serve-all", "foreign-command")
        if scenario.startswith("windows-policy-") and registered:
            payload = json.loads(content)
            task = ET.fromstring(payload["xml"])
            count = task.find("{*}Settings/{*}RestartOnFailure/{*}Count")
            assert count is not None
            count.text = "3"
            payload["xml"] = ET.tostring(task, encoding="unicode")
            payload["principal_matches"] = scenario != "windows-policy-foreign-principal"
            content = json.dumps(payload)
        missing = system == "Darwin" and not registered
        return subprocess.CompletedProcess(
            args,
            113 if missing else 0,
            content,
            f'Could not find service "{identity}" in domain for user gui: 1000' if missing else "",
        )

    monkeypatch.setattr(native_lifecycle, "_expected_native_definition", expected)
    monkeypatch.setattr(native_lifecycle, "_native_command", native_command)
    actions: list[list[str]] = []

    def fake_run(_executable: str, args: list[str], env: dict[str, str] | None = None) -> dict:
        nonlocal baseline_content, wired_content
        actions.append(args)
        if "--dry-run" in args:
            slug, key = {
                "Windows": ("windows", "xml"),
                "Darwin": ("macos", "plist"),
                "Linux": ("linux", "unit"),
            }[native_lifecycle.platform.system()]
            label = args[args.index("--label") + 1]
            spec = ServiceSpec(
                program="/synthetic/python",
                args=[
                    "-m",
                    "brains",
                    "serve-all",
                    "--gateway-port",
                    "24001",
                    "--mcp-port",
                    "24002",
                ],
                working_dir=str(home),
                user="synthetic-user",
                label=label,
                state_dir=str(runtime / "state"),
                gateway_port=24001,
                mcp_port=24002,
            )
            identity = native_lifecycle.native_service_identity(slug, label)
            definition_path = {
                "Windows": runtime / "state/service" / f"{identity}.xml",
                "Darwin": home / "Library/LaunchAgents" / f"{identity}.plist",
                "Linux": home / ".config/systemd/user" / identity,
            }[system]
            content = {
                "Windows": windows.render_task_xml,
                "Darwin": macos.render_plist,
                "Linux": linux.render_unit,
            }[system](spec)
            return {
                "action": "would-install",
                "platform": slug,
                "label": native_lifecycle.native_service_identity(
                    slug, args[args.index("--label") + 1]
                ),
                "definition": str(definition_path),
                "command": spec.command_line,
                key: content,
            }
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
            definition_path = Path(expected_definition["definition_path"])
            definition_path.parent.mkdir(parents=True, exist_ok=True)
            definition_path.write_text(
                expected_definition["content"],
                encoding="utf-16" if system == "Windows" else "utf-8",
            )
            if scenario == "prepare-partial-definition":
                raise native_lifecycle.EvidenceFailure("synthetic registration failure")
            state.update(installed=True, running=True)
            if scenario == "config-drift":
                config_path.write_bytes(b"unexpected client edit")
            if scenario in {
                "prepare-partial-install",
                "manager-cycle-partial-install",
                "config-drift",
                "native-drift",
            }:
                raise native_lifecycle.EvidenceFailure("synthetic partial install")
        elif args[:2] == ["service", "stop"]:
            state["running"] = False
        elif args[:2] in (["service", "start"], ["service", "restart"]):
            state["running"] = True
        elif args[:2] == ["service", "uninstall"]:
            state.update(installed=False, running=False)
            Path(expected_definition["definition_path"]).unlink()
        return {"ok": True}

    pids = iter((101, 101, 103, 104, 105) if scenario == "reuse-pid" else (101, 102, 103, 104, 105))

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
            "installed": state["installed"] and (system != "Darwin" or state["running"]),
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

    executable = (
        tmp_path
        / "venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("brains-ai.exe" if os.name == "nt" else "brains-ai")
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable")
    if scenario in {
        "prepare-partial-install",
        "prepare-partial-definition",
        "manager-cycle-partial-install",
        "config-drift",
        "native-drift",
        "windows-policy-drift",
        "windows-policy-foreign-principal",
    }:
        phase = "manager-cycle" if scenario == "manager-cycle-partial-install" else "prepare"
        code, result = _lifecycle_main(monkeypatch, tmp_path, phase, executable, provenance)
        assert code == 1 and result["passed"] is False
        rollback = result["failure_cleanup"]
        if runtime.exists():
            assert "plan_core_sha256" not in json.loads(native_lifecycle._plan_path().read_text())
        if scenario in {"native-drift", "windows-policy-foreign-principal"}:
            assert rollback["native_error_type"] == "EvidenceFailure"
            assert state["installed"] is True
            assert not any(args[:2] == ["service", "uninstall"] for args in actions)
        else:
            assert rollback["native_removed"] is True
            assert state["installed"] is False
        if scenario == "config-drift":
            assert rollback["configuration_error_type"] == "EvidenceFailure"
            assert config_path.read_bytes() == b"unexpected client edit"
        else:
            assert rollback["configuration_removed"] is True
            assert not config_path.exists()
        assert rollback["runtime_root_removed"] is (
            scenario not in {"config-drift", "native-drift", "windows-policy-foreign-principal"}
        )
        assert runtime.exists() is (
            scenario in {"config-drift", "native-drift", "windows-policy-foreign-principal"}
        )
        diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert diagnostics[0]["phase"] == phase
        assert diagnostics[0]["last_step"] == (
            "installed" if scenario.startswith("windows-policy-") else "adapter-wired"
        )
        if scenario.startswith("windows-policy-"):
            assert diagnostics[0]["task_recovery_policy"]["actual"]["restart_count"] == 3
            assert diagnostics[0]["task_recovery_policy"]["expected"]["restart_count"] == 9999
        assert diagnostics[-1]["stage"] == "rollback-outcome"
        assert (
            diagnostics[-1]["cleanup"]["runtime_root_removed"] == rollback["runtime_root_removed"]
        )
        assert "synthetic partial install" not in json.dumps(diagnostics)
        return
    if scenario == "reuse-pid":
        with pytest.raises(native_lifecycle.EvidenceFailure, match="reused"):
            native_lifecycle.prepare(str(executable), "1" * 40, "codex", provenance)
        return
    if scenario == "manager-cycle-cleanup":
        code, prepared = _lifecycle_main(
            monkeypatch, tmp_path / "cycle", "manager-cycle", executable, provenance
        )
        assert code == 0 and prepared["passed"] is True
    else:
        prepared = native_lifecycle.prepare(str(executable), "1" * 40, "codex", provenance)
    assert prepared["boundary"]["boot_changed"] is False
    prepared["passed"] = True
    prepare_path = tmp_path / "native-service-prepare.json"
    prepare_path.write_text(json.dumps(prepared), encoding="utf-8")
    prepare_sha256 = hashlib.sha256(prepare_path.read_bytes()).hexdigest()
    original_plan = json.loads(native_lifecycle._plan_path().read_text(encoding="utf-8"))
    recovery_diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    if system == "Windows":
        assert len(recovery_diagnostics) == 1
        assert recovery_diagnostics[0]["diagnostic"] == "native-windows-recovery"
        assert recovery_diagnostics[0]["recovered"] is True
    else:
        assert recovery_diagnostics == []
    assert "recovery_before" not in original_plan
    if scenario == "manager-cycle-cleanup" or scenario.startswith("runtime-"):
        if scenario == "runtime-marker-drift":
            (runtime / "journey-owner.json").write_text("{}")
        elif scenario == "runtime-directory-drift":
            (runtime / "foreign-directory").mkdir()
        elif scenario.startswith("runtime-"):
            foreign = runtime / "foreign.txt"
            foreign.write_bytes(b"unowned")
            if scenario == "runtime-link-drift":
                original_is_symlink = Path.is_symlink
                monkeypatch.setattr(
                    Path, "is_symlink", lambda self: self == foreign or original_is_symlink(self)
                )
        else:
            (runtime / "state/brains.db").write_bytes(b"mutable database")
            (runtime / "state/sessions/service.log.1").write_bytes(b"rotated log")
        code, result = _lifecycle_main(
            monkeypatch,
            tmp_path,
            "cleanup",
            executable,
            provenance,
            "--prior-record",
            str(prepare_path),
        )
        if scenario.startswith("runtime-"):
            assert code == 1 and runtime.exists()
            diagnostic = json.loads(capsys.readouterr().err)
            assert diagnostic["stage"] == "cleanup"
            assert diagnostic["cleanup"] == {
                "native_removed": True,
                "configuration_removed": True,
                "runtime_root_removed": False,
            }
            if scenario in {"runtime-drift", "runtime-link-drift"}:
                assert (runtime / "foreign.txt").read_bytes() == b"unowned"
        else:
            assert code == 0 and result["passed"] is True
            assert result["cleanup"]["runtime_root_removed"] is True
            assert not runtime.exists()
        return
    if scenario == "cleanup-config-drift":
        config_path.write_bytes(b"unexpected client edit")
        code, result = _lifecycle_main(
            monkeypatch,
            tmp_path,
            "cleanup",
            executable,
            provenance,
            "--prior-record",
            str(prepare_path),
        )
        assert code == 1 and result["passed"] is False
        assert state["installed"] is False
        assert config_path.read_bytes() == b"unexpected client edit"
        assert not any(args[0] == "unwire" for args in actions)
        return
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
    if scenario in {"verify-mid-step", "verify-restoration"}:
        original_record = native_lifecycle._record

        def interrupted_record(plan: dict, step: str, evidence: dict) -> None:
            original_record(plan, step, evidence)
            assert native_lifecycle._plan_path().read_bytes() == before
            target = (
                "boundary-verified" if scenario == "verify-mid-step" else "configuration-restored"
            )
            if step == target:
                raise native_lifecycle.EvidenceFailure("synthetic interrupted verification")

        monkeypatch.setattr(native_lifecycle, "_record", interrupted_record)
        before = native_lifecycle._plan_path().read_bytes()
        code, result = _lifecycle_main(
            monkeypatch,
            tmp_path,
            "verify",
            executable,
            provenance,
            "--prepare-record",
            str(prepare_path),
            "--prepare-record-sha256",
            prepare_sha256,
        )
        assert code == 1
        assert result["failure_cleanup"]["native_removed"] is True
        assert result["failure_cleanup"]["configuration_removed"] is True
        assert not runtime.exists()
        assert (
            native_lifecycle._validated_plan_digest(original_plan) == prepared["plan_core_sha256"]
        )
        return
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
    assert json.loads(native_lifecycle._plan_path().read_text()) == original_plan
    verified["passed"] = True
    verify_path = tmp_path / "native-service-verify.json"
    native_lifecycle._write_result(verify_path, verified, passed=True)
    prior = native_lifecycle._prior_normal_record(
        verify_path, provenance["binding_sha256"], "1" * 40, "codex"
    )
    assert prior["record"] == verified
    verify_bytes = verify_path.read_bytes()
    for mutation in (
        "missing-binding",
        "malformed-binding",
        "unknown-key",
        "sha",
        "journey",
        "plan",
        "resealed-plan",
    ):
        changed = json.loads(json.dumps(verified))
        if mutation == "missing-binding":
            del changed["prepare_record_sha256"]
        elif mutation == "malformed-binding":
            changed["prepare_record_sha256"] = None
        elif mutation == "unknown-key":
            changed["unknown"] = True
        elif mutation == "sha":
            changed["prepare_record_sha256"] = "0" * 64
        elif mutation == "journey":
            changed["journey"] = native_lifecycle._journey("1" * 40, "codex", "f" * 64)
        elif mutation == "plan":
            changed["plan_core_sha256"] = "0" * 64
        else:
            tampered = json.loads(json.dumps(original_plan))
            tampered["steps"].append({"unexpected": True})
            tampered["plan_core_sha256"] = native_evidence.canonical_sha256(
                {key: tampered[key] for key in native_lifecycle.PLAN_CORE_FIELDS}
            )
            native_lifecycle._plan_path().write_text(json.dumps(tampered))
        verify_path.write_text(json.dumps(changed))
        with monkeypatch.context() as patch:
            fallback = Mock(side_effect=AssertionError("untrusted cleanup"))
            patch.setattr(native_lifecycle, "cleanup", fallback)
            patch.setattr(native_lifecycle, "_rollback", fallback)
            code, rejected = _lifecycle_main(
                patch,
                tmp_path / mutation,
                "cleanup",
                executable,
                provenance,
                "--prior-record",
                str(verify_path),
                "--prepare-record",
                str(prepare_path),
                "--prepare-record-sha256",
                prepare_sha256,
            )
            assert code == 1 and rejected["passed"] is False
            fallback.assert_not_called()
        native_lifecycle._plan_path().write_text(json.dumps(original_plan))
    verify_path.write_bytes(verify_bytes)
    code, result = _lifecycle_main(
        monkeypatch,
        tmp_path,
        "cleanup",
        executable,
        provenance,
        "--prior-record",
        str(verify_path),
        "--prepare-record",
        str(prepare_path),
        "--prepare-record-sha256",
        prepare_sha256,
    )
    assert code == 0 and result["passed"] is True
    assert result["cleanup"]["definition_removed"] is True
    assert result["cleanup"]["initial_client_home_restored"] is True
    assert result["cleanup"]["runtime_root_removed"] is True
    assert result["cleanup"]["prepare_record_sha256"] == prepare_sha256
    assert result["plan_core_sha256"] == prepared["plan_core_sha256"]
    assert sum(args[:2] == ["service", "uninstall"] for args in actions) == 1
    assert not runtime.exists()
    assert not config_path.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_native_prior_record_requires_exact_phase_schema(tmp_path: Path, phase: str) -> None:
    record = _service_record("1" * 40)
    if phase == "prepare":
        record = _prepare_record(record)
    binding = record["provenance"]["binding_sha256"]
    record["journey"] = native_lifecycle._journey("1" * 40, "codex", binding)
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert native_lifecycle._prior_normal_record(path, binding, "1" * 40, "codex")["phase"] == phase
    mutations = [{key: value for key, value in record.items() if key != field} for field in record]
    mutations.append({**record, "unknown": True})
    if phase == "prepare":
        mutations.append({**record, "prepare_record_sha256": "a" * 64})
    else:
        mutations.extend(
            {**record, "prepare_record_sha256": value}
            for value in (
                None,
                True,
                123,
                [],
                {},
                "",
                "a" * 63,
                "a" * 65,
                "g" * 64,
                "A" * 64,
                "a" * 64 + "\n",
            )
        )
    for changed in mutations:
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(native_lifecycle.EvidenceFailure, match="not provenance-bound"):
            native_lifecycle._prior_normal_record(path, binding, "1" * 40, "codex")


@pytest.mark.parametrize("phase", ["prepare", "manager-cycle", "verify", "cleanup"])
@pytest.mark.parametrize("failure", ["guard", "provenance"])
def test_native_main_untrusted_failure_never_uses_preexisting_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan_path = runtime / "journey-plan.json"
    plan_path.write_text('{"executable":"foreign","label":"brains-serve-all"}')
    before = plan_path.read_bytes()
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(runtime))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(runtime / "state"))
    if failure == "guard":
        monkeypatch.delenv("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", raising=False)
    else:
        monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", native_lifecycle.ACKNOWLEDGEMENT)
        monkeypatch.setattr(native_lifecycle, "_guard", lambda _phase: None)
    executable = (
        tmp_path
        / "venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("brains-ai.exe" if os.name == "nt" else "brains-ai")
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic")
    calls = Mock(side_effect=AssertionError("no native or config mutation authorized"))
    for name in ("cleanup", "_rollback", "_run", "_native_observation", "_remove_synthetic_config"):
        monkeypatch.setattr(native_lifecycle, name, calls)
    code, result = _lifecycle_main(
        monkeypatch,
        tmp_path,
        phase,
        executable,
        native_lifecycle.EvidenceFailure("untrusted provenance"),
    )
    assert code == 1 and result["passed"] is False
    assert "failure_cleanup" not in result
    calls.assert_not_called()
    assert plan_path.read_bytes() == before
    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert captured.out == ""
    assert diagnostic["phase"] == phase
    assert diagnostic["stage"] == failure
    assert diagnostic["error_type"] == "EvidenceFailure"
    assert diagnostic["error_code"] == (
        "disposable-host-acknowledgement-is-absent"
        if failure == "guard"
        else "unclassified-evidence-failure"
    )
    assert "untrusted provenance" not in captured.err


def test_native_diagnostic_taxonomy_covers_all_owned_failure_literals() -> None:
    for path, exception_name in (
        (_LIFECYCLE_PATH, "EvidenceFailure"),
        (_NATIVE_EVIDENCE_PATH, "ProvenanceFailure"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == exception_name
            ):
                message = node.exc.args[0]
                assert isinstance(message, ast.Constant), "dynamic failure messages need review"
                assert message.value in native_lifecycle._DIAGNOSTIC_CODES
    assert len(native_lifecycle._DIAGNOSTIC_CODES) == len(native_lifecycle._DIAGNOSTIC_MESSAGES)
    assert len(set(native_lifecycle._DIAGNOSTIC_CODES.values())) == len(
        native_lifecycle._DIAGNOSTIC_CODES
    )


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Task Scheduler observation failed", "task-scheduler-observation-failed"),
        ("launchd observation failed", "launchd-observation-failed"),
        ("systemd observation failed", "systemd-observation-failed"),
        ("client configuration already exists", "client-configuration-already-exists"),
        ("unexpected runtime directory", "unexpected-runtime-directory"),
        ("systemd observation failed /private/synthetic-token", "unclassified-evidence-failure"),
    ],
)
def test_native_main_failure_diagnostics_are_allowlisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
    code: str,
) -> None:
    monkeypatch.setattr(native_lifecycle, "_guard", lambda _phase: None)
    monkeypatch.setattr(
        native_lifecycle, "prepare", Mock(side_effect=native_lifecycle.EvidenceFailure(message))
    )
    executable = tmp_path / "venv" / "bin" / "synthetic"
    monkeypatch.setattr(native_lifecycle.Path, "is_file", lambda _path: True)
    exit_code, record = _lifecycle_main(
        monkeypatch, tmp_path, "manager-cycle", executable, {"binding_sha256": "f" * 64}
    )
    assert exit_code == 1
    assert record == {"phase": "manager-cycle", "passed": False, "error_type": "EvidenceFailure"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "diagnostic": "native-service-failure",
        "phase": "manager-cycle",
        "stage": "lifecycle",
        "operation": None,
        "command": None,
        "last_step": None,
        "error_type": "EvidenceFailure",
        "error_code": code,
    }
    assert "/private/synthetic-token" not in captured.err
    assert str(tmp_path) not in captured.err


@pytest.mark.parametrize("failure", ["stale-output", "provenance", "export", "unsafe-exception"])
def test_native_main_diagnoses_preflight_provenance_and_export_without_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    secret = "/private/synthetic-token\nBRAINS_MCP_BEARER_TOKEN=synthetic-secret"
    monkeypatch.setattr(native_lifecycle, "_guard", lambda _phase: None)
    monkeypatch.setattr(native_lifecycle.Path, "is_file", lambda _path: True)
    prepare = Mock(return_value={"phase": "prepare"})
    monkeypatch.setattr(native_lifecycle, "prepare", prepare)
    provenance = {"binding_sha256": "f" * 64}
    if failure == "stale-output":
        (tmp_path / "result.json").write_text('{"original":true}', encoding="utf-8")
        expected = (
            "output-preflight",
            "ProvenanceFailure",
            "native-evidence-output-already-exists",
        )
    elif failure == "provenance":
        provenance = native_evidence.ProvenanceFailure("checked-out candidate is not clean")
        expected = ("provenance", "ProvenanceFailure", "checked-out-candidate-is-not-clean")
    elif failure == "export":
        monkeypatch.setattr(native_lifecycle, "_write_result", Mock(side_effect=OSError(secret)))
        expected = ("result-export", "OSError", "unexpected-error")
    else:
        prepare.side_effect = subprocess.CalledProcessError(2, secret, output=secret, stderr=secret)
        expected = ("lifecycle", "CalledProcessError", "unexpected-error")
    if failure == "export":
        # The normal helper reads the record after main; leave that read synthetic
        # because this case intentionally cannot write a record.
        monkeypatch.setattr(native_lifecycle.Path, "read_text", lambda *_a, **_kw: "{}")
    exit_code, result = _lifecycle_main(
        monkeypatch, tmp_path, "manager-cycle", tmp_path / "venv/bin/synthetic", provenance
    )
    assert exit_code == 1
    if failure == "stale-output":
        assert result == {"original": True}
    if failure in {"stale-output", "provenance"}:
        prepare.assert_not_called()
    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert (diagnostic["stage"], diagnostic["error_type"], diagnostic["error_code"]) == expected
    assert captured.out == ""
    assert "synthetic-token" not in captured.err
    assert "synthetic-secret" not in captured.err
    assert str(tmp_path) not in captured.err


def test_native_diagnostics_redact_unknown_class_steps_and_cleanup_content(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsafe = "synthetic-secret"
    error = type(unsafe, (RuntimeError,), {})(unsafe)
    native_lifecycle._diagnose(
        error,
        phase=unsafe,
        stage=unsafe,
        context={"plan": {"steps": [{"step": unsafe}]}},
        outcomes={"native_removed": True, "configuration_removed": unsafe, "unknown": unsafe},
    )
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["phase"] == diagnostic["stage"] == "unknown"
    assert diagnostic["error_type"] == "Exception"
    assert diagnostic["error_code"] == "unexpected-error"
    assert diagnostic["last_step"] is None
    assert diagnostic["cleanup"] == {
        "native_removed": True,
        "configuration_removed": False,
        "runtime_root_removed": False,
    }
    assert unsafe not in json.dumps(diagnostic)


def test_native_rollback_reports_independent_errors_and_retains_uncertain_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = {
        "phase": "manager-cycle",
        "native_armed": True,
        "config_snapshot": {},
        "config_directories": [],
        "plan": {
            "adapter": "codex",
            "original_snapshot": {},
            "steps": [{"step": "adapter-wired"}],
        },
    }
    monkeypatch.setattr(
        native_lifecycle,
        "_uninstall_owned",
        Mock(side_effect=native_lifecycle.EvidenceFailure("systemd observation failed")),
    )
    monkeypatch.setattr(
        native_lifecycle,
        "_remove_synthetic_config",
        Mock(side_effect=PermissionError("/private/synthetic-secret")),
    )
    remove_runtime = Mock(side_effect=AssertionError("runtime removal not authorized"))
    monkeypatch.setattr(native_lifecycle, "_remove_runtime", remove_runtime)
    result = native_lifecycle._rollback(context)
    remove_runtime.assert_not_called()
    assert result == {
        "native_error_type": "EvidenceFailure",
        "configuration_error_type": "PermissionError",
        "runtime_root_removed": False,
    }
    diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [item["stage"] for item in diagnostics] == ["cleanup-native", "cleanup-configuration"]
    assert [item["error_code"] for item in diagnostics] == [
        "systemd-observation-failed",
        "unexpected-error",
    ]
    assert all(item["last_step"] == "adapter-wired" for item in diagnostics)
    assert "synthetic-secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize("stdout", ["synthetic-secret", '{"ok":false}'])
def test_native_command_diagnostic_reports_only_allowlisted_verb(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdout: str,
) -> None:
    monkeypatch.setattr(
        native_lifecycle.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 2, stdout, "synthetic-secret")),
    )
    with pytest.raises(native_lifecycle.EvidenceFailure) as raised:
        native_lifecycle._run(
            "/private/synthetic-secret", ["service", "install", "synthetic-secret"]
        )
    native_lifecycle._diagnose(raised.value, phase="prepare", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["command"] == "service-install"
    assert diagnostic["operation"] == "_run"
    assert diagnostic["error_code"] == (
        "command-returned-a-non-json-result"
        if stdout == "synthetic-secret"
        else "command-reported-failure"
    )
    if stdout == "synthetic-secret":
        assert "service_error_code" not in diagnostic
    else:
        assert diagnostic["service_error_code"] == "unclassified-service-error"
    assert "synthetic-secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("action", ["stop", "uninstall", "restart"])
@pytest.mark.parametrize("backend_code", sorted(native_lifecycle._SERVICE_ERROR_CODES))
@pytest.mark.parametrize("returncode", [0, 1])
def test_native_command_diagnostic_preserves_allowlisted_service_error(
    monkeypatch, capsys, action, backend_code, returncode
) -> None:
    secret = "/private/synthetic-secret\nBRAINS_API_KEY=synthetic-secret"
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            returncode,
            json.dumps({"ok": False, "error_code": backend_code, "detail": secret}),
            secret,
        )
    )
    monkeypatch.setattr(native_lifecycle.subprocess, "run", run)
    with pytest.raises(native_lifecycle.EvidenceFailure) as raised:
        native_lifecycle._run(secret, ["service", action, "--label", secret])
    native_lifecycle._diagnose(
        raised.value,
        phase="manager-cycle",
        stage="lifecycle",
        context={"diagnostic_steps": [{"step": "installed"}]},
    )
    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert diagnostic["error_code"] == "command-reported-failure"
    assert diagnostic["service_error_code"] == backend_code
    assert diagnostic["command"] == "service-" + action
    assert diagnostic["last_step"] == "installed"
    assert "synthetic-secret" not in captured.err
    assert captured.out == ""
    run.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False},
        {"ok": False, "error_code": None},
        {"ok": False, "error_code": "pidfile-changed /private/synthetic-secret"},
        {"ok": False, "error_code": ["pidfile-changed", "synthetic-secret"]},
        {"ok": False, "error_code": {"pidfile-changed": "synthetic-secret"}},
        {"ok": False, "error_code": 1},
        {"ok": False, "error_code": True},
        {"ok": False, "detail": "pidfile-changed"},
        {"ok": False, "rollback": {"error_code": "pidfile-changed"}},
    ],
)
def test_native_command_diagnostic_rejects_unreviewed_service_error(
    monkeypatch, capsys, payload
) -> None:
    monkeypatch.setattr(
        native_lifecycle.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess([], 1, json.dumps(payload), "synthetic-secret")
        ),
    )
    with pytest.raises(native_lifecycle.EvidenceFailure) as raised:
        native_lifecycle._run("synthetic-secret", ["service", "stop"])
    native_lifecycle._diagnose(raised.value, phase="manager-cycle", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["error_code"] == "command-reported-failure"
    assert diagnostic["service_error_code"] == "unclassified-service-error"
    assert "synthetic-secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("args", [["wire"], ["service", "synthetic-secret"]])
def test_native_command_diagnostic_does_not_attribute_nonservice_error(
    monkeypatch, capsys, args
) -> None:
    monkeypatch.setattr(
        native_lifecycle.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, '{"ok": false, "error_code": "pidfile-changed"}', ""
            )
        ),
    )
    with pytest.raises(native_lifecycle.EvidenceFailure) as raised:
        native_lifecycle._run("synthetic", args)
    native_lifecycle._diagnose(raised.value, phase="manager-cycle", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert "service_error_code" not in diagnostic
    assert "synthetic-secret" not in json.dumps(diagnostic)


@pytest.mark.parametrize("observation", ["unavailable", "foreign", "wrong-identity"])
def test_native_prepare_requires_positive_absence_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(runtime))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(runtime / "state"))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_DISPOSABLE", native_lifecycle.ACKNOWLEDGEMENT)
    monkeypatch.setattr(native_lifecycle, "_boot_marker", lambda: "a" * 64)
    ports = iter((24001, 24002))
    monkeypatch.setattr(native_lifecycle, "_port", lambda: next(ports))
    monkeypatch.setattr(
        native_lifecycle,
        "_native_command",
        lambda *_args, **_kw: subprocess.CompletedProcess([], 2, "", "access denied"),
    )
    if observation != "unavailable":
        monkeypatch.setattr(
            native_lifecycle,
            "_native_observation",
            lambda _label: {
                "label": "foreign",
                "definition": "foreign" if observation == "foreign" else None,
                "registered": True,
            },
        )
    mutation = Mock(side_effect=AssertionError("mutation before ownership"))
    monkeypatch.setattr(native_lifecycle, "_run", mutation)
    monkeypatch.setattr(native_lifecycle, "_seed", mutation)
    context: dict = {}
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle.prepare(
            "synthetic", "1" * 40, "codex", {"binding_sha256": "f" * 64}, rollback_context=context
        )
    mutation.assert_not_called()
    assert context == {}
    assert not (home / ".codex").exists()


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
@pytest.mark.parametrize(
    "case", ["absent", "owned", "unloaded", "error", "foreign", "local-drift", "identity"]
)
def test_native_observation_parses_manager_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, case: str
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: system)
    monkeypatch.setattr(native_lifecycle.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path / "runtime/state"))
    label = "brains-serve-all-evidence-11111111"
    slug = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[system]
    identity = native_lifecycle.native_service_identity(slug, label)
    spec = ServiceSpec(
        program="/synthetic/python",
        label=label,
        user="synthetic-user",
        working_dir="/synthetic/home",
        state_dir="/synthetic/state",
    )
    path = {
        "Windows": tmp_path / "runtime/state/service" / f"{identity}.xml",
        "Darwin": tmp_path / "Library/LaunchAgents" / f"{identity}.plist",
        "Linux": tmp_path / ".config/systemd/user" / identity,
    }[system]
    content = {
        "Windows": windows.render_task_xml,
        "Darwin": macos.render_plist,
        "Linux": linux.render_unit,
    }[system](spec)
    expected = {"label": identity, "definition_path": str(path), "content": content}
    if case != "absent":
        path.parent.mkdir(parents=True)
        path.write_text(
            content + ("foreign" if case == "local-drift" else ""),
            encoding="utf-16" if system == "Windows" else "utf-8",
        )
    registered = case not in {"absent", "unloaded"}
    output = _mock_native_response(system, identity, expected, registered)
    if case == "foreign":
        if system == "Windows":
            payload = json.loads(output)
            payload["xml"] = payload["xml"].replace("SYNTHETIC", "FOREIGN")
            output = json.dumps(payload)
        else:
            output = output.replace("/synthetic/python", "/foreign/python")
    if case == "identity":
        output = output.replace(identity, identity + "-foreign")
    missing = system == "Darwin" and not registered
    error = f'Could not find service "{identity}" in domain for user gui: 1000' if missing else ""
    code = 113 if missing else 0
    if case == "error":
        code, error = 2, "access denied"
    monkeypatch.setattr(
        native_lifecycle,
        "_native_command",
        lambda *_args, **_kw: subprocess.CompletedProcess([], code, output, error),
    )
    if case in {"error", "foreign", "local-drift", "identity"}:
        with pytest.raises(native_lifecycle.EvidenceFailure):
            native_lifecycle._native_observation(label, expected)
    else:
        result = native_lifecycle._native_observation(label, expected)
        assert result["registered"] is registered
        assert result["definition"] == (None if case == "absent" else expected)
        if case != "absent":
            with pytest.raises(native_lifecycle.EvidenceFailure):
                native_lifecycle._native_observation(label)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("normalized", None),
        ("sid", None),
        ("executable", "registered task executable differs"),
        ("arguments", "registered task arguments differ"),
        ("working-directory", "registered task working directory differs"),
        ("missing-working-directory", "registered task working directory differs"),
        ("extra-action", "registered task action structure differs"),
        ("principal", "registered task principal or identity differs"),
        ("trigger-principal", "registered task trigger principal differs"),
        ("disabled-trigger", "registered task trigger definition differs"),
        ("extra-trigger", "registered task trigger definition differs"),
        ("trigger-limit", "registered task trigger definition differs"),
        ("elevated", "registered task principal or identity differs"),
        ("context", "registered task principal or identity differs"),
    ],
)
def test_native_windows_semantic_definition_and_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
    message: str | None,
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: "Windows")
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(tmp_path / "runtime"))
    label = "brains-serve-all-evidence-11111111"
    identity = native_lifecycle.native_service_identity("windows", label)
    spec = ServiceSpec(
        program="C:/Synthetic Runtime/pythonw.exe",
        args=["-m", "brains", "serve-all"],
        label=label,
        user="synthetic-user",
        working_dir="C:/Synthetic Home",
    )
    content = windows.render_task_xml(spec)
    path = tmp_path / "runtime/state/service" / f"{identity}.xml"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-16")
    expected = {"label": identity, "definition_path": str(path), "content": content}
    payload = json.loads(_mock_native_response("Windows", identity, expected, True))
    task = ET.fromstring(payload["xml"])
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    actions = task.find(ns + "Actions")
    triggers = task.find(ns + "Triggers")
    assert actions is not None and triggers is not None
    if mutation in {"executable", "arguments", "working-directory"}:
        field = {
            "executable": "Command",
            "arguments": "Arguments",
            "working-directory": "WorkingDirectory",
        }[mutation]
        node = actions[0].find(ns + field)
        assert node is not None
        node.text = "synthetic-secret-foreign-value"
    elif mutation == "missing-working-directory":
        node = actions[0].find(ns + "WorkingDirectory")
        assert node is not None
        actions[0].remove(node)
    elif mutation == "extra-action":
        ET.SubElement(actions, ns + "Exec")
    elif mutation == "extra-trigger":
        ET.SubElement(triggers, ns + "BootTrigger")
    elif mutation == "disabled-trigger":
        ET.SubElement(triggers[0], ns + "Enabled").text = "false"
    elif mutation == "trigger-limit":
        node = triggers[0].find(ns + "ExecutionTimeLimit")
        assert node is not None
        node.text = "PT1H"
    elif mutation == "elevated":
        node = task.find(ns + "Principals/" + ns + "Principal/" + ns + "RunLevel")
        assert node is not None
        node.text = "HighestAvailable"
    elif mutation == "context":
        actions.set("Context", "ForeignPrincipal")
    elif mutation in {"principal", "trigger-principal", "sid"}:
        node = task.find(ns + "Principals/" + ns + "Principal/" + ns + "UserId")
        trigger_user = triggers[0].find(ns + "UserId")
        assert node is not None and trigger_user is not None
        node.text = "S-1-5-21-100-200-300-1001"
        trigger_user.text = "SYNTHETIC-DOMAIN\\synthetic-user"
        payload["principal_matches"] = mutation != "principal"
        payload["trigger_matches"] = mutation != "trigger-principal"
    settings = task.find(ns + "Settings")
    assert settings is not None
    ET.SubElement(settings, ns + "UseUnifiedSchedulingEngine").text = "true"
    payload["xml"] = ET.tostring(task, encoding="unicode")
    commands = []

    def query(args: list[str], *, env: dict) -> subprocess.CompletedProcess:
        commands.append((args, env))
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(native_lifecycle, "_native_command", query)
    if message is None:
        assert native_lifecycle._native_observation(label, expected)["registered"] is True
    else:
        with pytest.raises(native_lifecycle.EvidenceFailure, match=message) as caught:
            native_lifecycle._native_observation(label, expected)
        native_lifecycle._diagnose(caught.value, phase="prepare", stage="lifecycle")
        diagnostic = json.loads(capsys.readouterr().err)
        assert diagnostic["error_code"] == native_lifecycle._DIAGNOSTIC_CODES[message]
        assert "synthetic-secret" not in json.dumps(diagnostic)
        assert str(tmp_path) not in json.dumps(diagnostic)
    assert "trigger_matches=$triggerMatch" in commands[0][0][-1]
    assert commands[0][1]["BRAINS_EVIDENCE_USER"] == "synthetic-user"


@pytest.mark.parametrize(
    "case",
    [
        "exit4",
        "exit1",
        "exit0-empty-array",
        "explicit-empty-array",
        "not-found-stderr",
        "bus-error",
        "empty",
        "loaded-exit4",
        "partial",
        "active",
        "wrong-id",
        "reload",
        "missing-fragment",
        "unexpected-exit",
        "duplicate-property",
        "unexpected-load-state",
        "loaded-missing-exec",
    ],
)
def test_native_systemd_not_found_requires_authoritative_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: "Linux")
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(tmp_path / "runtime"))
    label = "brains-serve-all-evidence-11111111"
    identity = native_lifecycle.native_service_identity("linux", label)
    output = _mock_native_response("Linux", identity, {}, False)
    code, error = (1 if case == "exit1" else 4), ""
    if case == "exit0-empty-array":
        code = 0
    elif case == "explicit-empty-array":
        output += "\nExecStart=\n"
    if case == "bus-error":
        error = "Failed to connect to bus: synthetic-private-detail"
    elif case == "not-found-stderr":
        error = f"Unit {identity} could not be found."
    elif case == "empty":
        output = ""
    elif case == "loaded-exit4":
        output = output.replace("LoadState=not-found", "LoadState=loaded")
    elif case == "partial":
        output = "LoadState=not-found\n"
    elif case == "active":
        output = output.replace("ActiveState=inactive", "ActiveState=active")
    elif case == "wrong-id":
        output = output.replace(identity, identity + "-foreign")
    elif case == "reload":
        output = output.replace("NeedDaemonReload=no", "NeedDaemonReload=yes")
    elif case == "missing-fragment":
        output = output.replace("FragmentPath=\n", "")
    elif case == "unexpected-exit":
        code = 2
    elif case == "duplicate-property":
        output += "\nLoadState=not-found\n"
    elif case == "unexpected-load-state":
        output = output.replace("LoadState=not-found", "LoadState=synthetic-private-detail")
    elif case == "loaded-missing-exec":
        code = 0
        output = output.replace("LoadState=not-found", "LoadState=loaded")
    command = Mock(return_value=subprocess.CompletedProcess([], code, output, error))
    monkeypatch.setattr(
        native_lifecycle,
        "_native_command",
        command,
    )
    if case in {"exit4", "exit1", "exit0-empty-array", "explicit-empty-array", "not-found-stderr"}:
        assert native_lifecycle._native_observation(label) == {
            "label": identity,
            "definition": None,
            "registered": False,
        }
    else:
        with pytest.raises(native_lifecycle.EvidenceFailure) as caught:
            native_lifecycle._native_observation(label)
        native_lifecycle._diagnose(caught.value, phase="prepare", stage="lifecycle")
        diagnostic = json.loads(capsys.readouterr().err)
        assert diagnostic["error_code"] != "systemd-observation-failed"
        assert diagnostic["systemd_query"]["properties_present"]["ExecStart"] is False
        assert "synthetic-private-detail" not in json.dumps(diagnostic)
        assert str(tmp_path) not in json.dumps(diagnostic)
        if case == "bus-error":
            assert diagnostic["systemd_query"]["stderr"] == "bus-failure"
        if case == "unexpected-exit":
            assert diagnostic["systemd_query"]["return_code"] == "other"
    assert "--all" in command.call_args.args[0]


def test_native_systemd_query_diagnostics_never_expose_property_values() -> None:
    secret = "synthetic-private-path-token"
    result = subprocess.CompletedProcess(
        [],
        123,
        "\n".join(
            [
                f"Id={secret}",
                f"LoadState={secret}",
                f"ActiveState={secret}",
                f"ExecStart={secret}",
                f"Environment={secret}",
                f"{secret}={secret}",
            ]
        ),
        secret,
    )
    diagnostic = native_lifecycle._systemd_query_diagnostic(result, "synthetic-unit")
    assert diagnostic["return_code"] == diagnostic["stderr"] == "other"
    assert diagnostic["load_state"] == diagnostic["active_state"] == "other"
    assert diagnostic["identity_matches"] is False
    assert diagnostic["properties_present"]["ExecStart"] is True
    assert secret not in json.dumps(diagnostic)


def test_native_workflow_queries_linux_shape_before_lifecycle() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/native-service-evidence.yml").read_text())
    steps = workflow["jobs"]["manager-cycle"]["steps"]
    diagnostic = next(
        step for step in steps if step.get("name") == "Diagnose Linux native unit query shape"
    )
    lifecycle = next(
        step
        for step in steps
        if step.get("name") == "Exercise native manager lifecycle without login claim"
    )
    assert steps.index(diagnostic) < steps.index(lifecycle)
    assert diagnostic["if"] == "runner.os == 'Linux'"
    assert "for show_all in (False, True)" in diagnostic["run"]
    assert "_systemd_query_diagnostic(result, identity)" in diagnostic["run"]
    assert "capture_output=True" in diagnostic["run"]
    assert "print(result" not in diagnostic["run"]


@pytest.mark.parametrize(
    "field",
    [
        "Enabled",
        "MultipleInstancesPolicy",
        "ExecutionTimeLimit",
        "RestartOnFailure/Interval",
        "RestartOnFailure/Count",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "changed", "duplicate"])
def test_native_windows_rejects_missing_or_changed_recovery_settings(
    field: str, mutation: str
) -> None:
    spec = ServiceSpec(program="C:/synthetic/pythonw.exe", label="brains-serve-all-evidence-test")
    wanted = ET.fromstring(windows.render_task_xml(spec))
    actual = ET.fromstring(ET.tostring(wanted))
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    parts = field.split("/")
    parent = actual.find("/".join(ns + part for part in ["Settings", *parts[:-1]]))
    assert parent is not None
    node = parent.find(ns + parts[-1])
    assert node is not None
    if mutation == "missing":
        parent.remove(node)
    elif mutation == "changed":
        node.text = "synthetic-foreign-setting"
    else:
        parent.append(ET.fromstring(ET.tostring(node)))
    identity = native_lifecycle.native_service_identity("windows", spec.label)
    if mutation == "missing" and field in {"Enabled", "MultipleInstancesPolicy"}:
        native_lifecycle._check_task_xml(actual, wanted, identity)
        return
    message = {
        "Enabled": "registered task recovery enabled differs",
        "MultipleInstancesPolicy": "registered task recovery multiple instances differs",
        "ExecutionTimeLimit": "registered task recovery execution limit differs",
        "RestartOnFailure/Interval": "registered task recovery restart interval differs",
        "RestartOnFailure/Count": "registered task recovery restart count differs",
    }[field]
    with pytest.raises(native_lifecycle.EvidenceFailure, match=message):
        native_lifecycle._check_task_xml(actual, wanted, identity)
    native_lifecycle._check_task_xml(actual, wanted, identity, check_recovery=False)


@pytest.mark.parametrize("zero", ["PT0S", "PT0H", "P0D", "P0DT0H0M0.000000S"])
def test_native_windows_accepts_equivalent_recovery_interval_and_harmless_defaults(
    zero: str,
) -> None:
    spec = ServiceSpec(program="C:/synthetic/pythonw.exe", label="brains-serve-all-evidence-test")
    wanted = ET.fromstring(windows.render_task_xml(spec))
    actual = ET.fromstring(ET.tostring(wanted))
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    settings = actual.find(ns + "Settings")
    assert settings is not None
    interval = settings.find(ns + "RestartOnFailure/" + ns + "Interval")
    enabled = settings.find(ns + "Enabled")
    assert interval is not None and enabled is not None
    interval.text = "P0DT0H1M0S"
    settings.remove(enabled)
    multiple = settings.find(ns + "MultipleInstancesPolicy")
    assert multiple is not None
    settings.remove(multiple)
    execution = settings.find(ns + "ExecutionTimeLimit")
    count = settings.find(ns + "RestartOnFailure/" + ns + "Count")
    assert execution is not None and count is not None
    execution.text = zero
    count.text = "+0009999"
    trigger_limit = ET.SubElement(
        actual.find(ns + "Triggers/" + ns + "LogonTrigger"), ns + "ExecutionTimeLimit"
    )
    trigger_limit.text = "P3D"
    ET.SubElement(settings, ns + "UseUnifiedSchedulingEngine").text = "true"
    native_lifecycle._check_task_xml(
        actual, wanted, native_lifecycle.native_service_identity("windows", spec.label)
    )


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT0S", 0),
        ("PT0H", 0),
        ("P0D", 0),
        ("PT60S", 60),
        ("PT1M", 60),
        ("P3D", 259200),
        ("PT72H", 259200),
        ("PT0.000001S", 0.000001),
        ("P", None),
        ("PT", None),
        ("P0DT", None),
        ("P1Y", None),
        ("P1M", None),
        ("P1W", None),
        ("-PT1S", None),
        ("PTNaNS", None),
        ("PT1e2S", None),
        ("PT0.0000001S", None),
        ("P999999999D", None),
        ("synthetic-secret", None),
    ],
)
def test_native_task_duration_is_finite_and_semantic(
    value: str, seconds: int | float | None
) -> None:
    assert native_lifecycle._duration_seconds(value) == seconds


@pytest.mark.parametrize("limit", ["P3D", "PT72H", "PT0.000001S", "PT1S"])
def test_native_task_nonzero_execution_limit_fails_policy_but_not_ownership(limit: str) -> None:
    spec = ServiceSpec(program="C:/synthetic/pythonw.exe", label="brains-serve-all-evidence-test")
    wanted = ET.fromstring(windows.render_task_xml(spec))
    actual = ET.fromstring(ET.tostring(wanted))
    node = actual.find("{*}Settings/{*}ExecutionTimeLimit")
    assert node is not None
    node.text = limit
    identity = native_lifecycle.native_service_identity("windows", spec.label)
    with pytest.raises(native_lifecycle.EvidenceFailure, match="recovery execution limit differs"):
        native_lifecycle._check_task_xml(actual, wanted, identity)
    native_lifecycle._check_task_xml(actual, wanted, identity, check_recovery=False)


def test_native_recovery_policy_reports_all_normalized_values_without_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = ServiceSpec(program="C:/synthetic/pythonw.exe", label="brains-serve-all-evidence-test")
    wanted = ET.fromstring(windows.render_task_xml(spec))
    actual = ET.fromstring(ET.tostring(wanted))
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    changes = {
        "Enabled": "false",
        "MultipleInstancesPolicy": "Queue",
        "ExecutionTimeLimit": "P3D",
        "RestartOnFailure/Interval": "PT2M",
        "RestartOnFailure/Count": "3",
    }
    for field, value in changes.items():
        node = actual.find("/".join(ns + part for part in ("Settings/" + field).split("/")))
        assert node is not None
        node.text = value
    ET.SubElement(actual, ns + "Data").text = "synthetic-secret"
    with pytest.raises(native_lifecycle.EvidenceFailure) as caught:
        native_lifecycle._check_task_xml(
            actual, wanted, native_lifecycle.native_service_identity("windows", spec.label)
        )
    native_lifecycle._diagnose(caught.value, phase="prepare", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["error_code"] == "registered-task-recovery-enabled-differs"
    assert diagnostic["task_recovery_policy"]["actual"] == {
        "enabled": False,
        "multiple_instances": "Queue",
        "execution_limit_seconds": 259200,
        "restart_interval_seconds": 120,
        "restart_count": 3,
    }
    assert diagnostic["task_recovery_policy"]["expected"]["execution_limit_seconds"] == 0
    assert "synthetic-secret" not in json.dumps(diagnostic)
    for field in changes:
        node = actual.find("/".join(ns + part for part in ("Settings/" + field).split("/")))
        assert node is not None
        node.text = "synthetic-secret"
    assert "synthetic-secret" not in json.dumps(native_lifecycle._task_recovery_policy(actual))


@pytest.mark.parametrize("field", ["Command", "Arguments", "WorkingDirectory", "UserId", "Context"])
def test_native_task_ownership_mode_never_accepts_foreign_identity(field: str) -> None:
    spec = ServiceSpec(program="C:/synthetic/pythonw.exe", label="brains-serve-all-evidence-test")
    wanted = ET.fromstring(windows.render_task_xml(spec))
    actual = ET.fromstring(ET.tostring(wanted))
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    if field == "Context":
        actual.find(ns + "Actions").set("Context", "foreign")
    elif field == "UserId":
        # Principal resolution itself is checked by _native_observation; an
        # absent user cannot bypass the structural identity check either.
        actual.find(ns + "Principals/" + ns + "Principal/" + ns + "UserId").text = ""
    else:
        actual.find(ns + "Actions/" + ns + "Exec/" + ns + field).text = "foreign"
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._check_task_xml(
            actual,
            wanted,
            native_lifecycle.native_service_identity("windows", spec.label),
            check_recovery=False,
        )


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "unknown-state",
        "string-result",
        "bool-result",
        "out-of-range",
        "negative-count",
        "extra-secret",
        "bad-relation",
        "non-json",
        "query-failed",
        "query-exception",
    ],
)
def test_native_windows_failure_snapshot_validates_and_redacts(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    payload = {
        "state": 4,
        "last_task_result": 267009,
        "running_instances": 1,
        "engine_pid_matches_recorded": False,
    }
    if case == "unknown-state":
        payload["state"] = "synthetic-secret"
    elif case == "string-result":
        payload["last_task_result"] = "synthetic-secret"
    elif case == "bool-result":
        payload["last_task_result"] = True
    elif case == "out-of-range":
        payload["last_task_result"] = 2**32
    elif case == "negative-count":
        payload["running_instances"] = -1
    elif case == "extra-secret":
        payload["command"] = "synthetic-secret"
    elif case == "bad-relation":
        payload["engine_pid_matches_recorded"] = "synthetic-secret"
    query = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            2 if case == "query-failed" else 0,
            "synthetic-secret" if case == "non-json" else json.dumps(payload),
            "synthetic-secret",
        )
    )
    if case == "query-exception":
        query.side_effect = OSError("synthetic-secret")
    monkeypatch.setattr(native_lifecycle, "_native_command", query)
    result = native_lifecycle._windows_scheduler_status(
        "brains-serve-all-evidence-test",
        {"service_pid": {"pid": 123, "confidence": "verified"}},
    )
    assert result == (payload if case == "valid" else {"available": False})
    assert "synthetic-secret" not in json.dumps(result)
    assert query.call_args.kwargs["env"]["BRAINS_EVIDENCE_PID"] == "123"
    assert query.call_args.kwargs["env"]["BRAINS_EVIDENCE_TASK"] == "BrainsServeAll-evidence-test"
    command = query.call_args.args[0]
    assert "EnginePID" in command[-1]
    assert "brains-serve-all-evidence-test" not in command[-1]


@pytest.mark.parametrize("healthy", [False, True])
def test_native_windows_scheduler_snapshot_only_on_failed_health_wait(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], healthy: bool
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: "Windows")
    label = "brains-serve-all-evidence-test"
    report = {
        "platform": "windows",
        "label": native_lifecycle.native_service_identity("windows", label),
        "state": "synthetic-secret",
        "installed": True,
        "healthy": healthy,
        "listeners": {"gateway": healthy, "mcp": healthy},
        "mcp_protocol": {"ready": healthy},
        "service_pid": {"pid": 123, "confidence": "verified"},
    }
    monkeypatch.setattr(native_lifecycle, "_status", lambda *_args: report)
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(native_lifecycle.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _delay: None)
    snapshot = {
        "state": 3,
        "last_task_result": -1073741510,
        "running_instances": 0,
        "engine_pid_matches_recorded": False,
    }
    query = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(snapshot), ""))
    monkeypatch.setattr(native_lifecycle, "_native_command", query)
    if healthy:
        assert native_lifecycle._wait_healthy("synthetic", label, timeout=1) == report
        query.assert_not_called()
        return
    with pytest.raises(
        native_lifecycle.EvidenceFailure, match="service did not become fully ready"
    ) as caught:
        native_lifecycle._wait_healthy("synthetic", label, timeout=1)
    query.assert_called_once()
    # The snapshot is already captured before main's failure diagnostic/rollback.
    native_lifecycle._diagnose(caught.value, phase="manager-cycle", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["task_scheduler"] == snapshot
    assert diagnostic["wait_status"]["pid_verified"] is True
    assert "synthetic-secret" not in json.dumps(diagnostic)
    assert "123" not in json.dumps(diagnostic)


def _task_event_xml(
    identity: str, event_id: int = 201, timestamp: str = "2026-09-07T00:00:01Z"
) -> str:
    ns = "{http://schemas.microsoft.com/win/2004/08/events/event}"
    event = ET.Element(ns + "Event")
    system = ET.SubElement(event, ns + "System")
    ET.SubElement(system, ns + "Provider", Name="Microsoft-Windows-TaskScheduler")
    ET.SubElement(system, ns + "EventID").text = str(event_id)
    ET.SubElement(system, ns + "TimeCreated", SystemTime=timestamp)
    data = ET.SubElement(event, ns + "EventData")
    for name, value in {
        "TaskName": "\\" + identity,
        "UserName": "synthetic-secret-user",
        "ActionName": "C:/synthetic-secret-command",
        "InstanceId": "synthetic-secret-guid",
        "ResultCode": "1",
        "ErrorCode": "0x80070002",
        "ErrorValue": "synthetic-secret-error",
    }.items():
        ET.SubElement(data, ns + "Data", Name=name).text = value
    return ET.tostring(event, encoding="unicode")


@pytest.mark.parametrize("recovered", [False, True])
def test_windows_recovery_capture_emits_bounded_private_event_projection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], recovered: bool
) -> None:
    start = 1788739200000  # 2026-09-07T00:00:00Z
    identity = "BrainsServeAll-evidence-test"
    snapshot = {"state": 3, "last_task_result": 1, "last_run_time_utc_ms": start - 1000}
    event_ids = [100, 101, 102, 110, 111, 129, 200, 201, 202, 203, 322, 999]
    rows = [_task_event_xml(identity, event_id) for event_id in event_ids]
    rows += [
        _task_event_xml(identity + "-foreign"),
        _task_event_xml(identity, timestamp="2026-09-06T23:59:59Z"),
        "malformed-secret",
    ]
    responses = iter(
        [
            {
                "snapshot": snapshot,
                "events": [],
                "events_available": False,
                "truncated": False,
                "window_end_ms": start,
            },
            {
                "snapshot": {
                    **snapshot,
                    "last_run_time_utc_ms": start + 1000 if recovered else start - 1000,
                },
                "events": rows,
                "events_available": True,
                "truncated": False,
                "window_end_ms": start + 2000,
            },
        ]
    )
    commands = []

    def query(args: list[str], *, env: dict) -> subprocess.CompletedProcess:
        commands.append((args, env))
        return subprocess.CompletedProcess(
            args, 0, json.dumps(next(responses)), "private-secret-stderr"
        )

    monkeypatch.setattr(native_lifecycle, "_native_command", query)
    before = native_lifecycle._windows_recovery_capture("brains-serve-all-evidence-test")
    native_lifecycle._emit_windows_recovery(
        "brains-serve-all-evidence-test", before, start, recovered
    )
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["recovered"] is recovered
    assert diagnostic["observed_new_run"] is recovered
    assert diagnostic["before"] == snapshot
    timeline = diagnostic["timeline"]
    assert timeline["source_count"] == 15 and timeline["matched_count"] == 12
    assert timeline["invalid_count"] == 13
    assert not timeline["events_unavailable"] and not timeline["truncated"]
    assert [row["event_id"] for row in timeline["events"]] == event_ids
    assert all(
        row["result_codes"] == [1] and row["error_codes"] == [2147942402]
        for row in timeline["events"]
    )
    encoded = json.dumps(diagnostic)
    assert "secret" not in encoded and identity not in encoded
    assert commands[0][1]["BRAINS_EVIDENCE_SINCE"] == ""
    assert commands[1][1]["BRAINS_EVIDENCE_SINCE"] == str(start)
    for args, _env in commands:
        assert "-MaxEvents 257" in args[-1] and "600000" in args[-1]
        assert identity not in args[-1]
        for forbidden in (
            "RegisterTask",
            "Stop-ScheduledTask",
            "Start-ScheduledTask",
            "wevtutil",
            ".Run(",
        ):
            assert forbidden not in args[-1]


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "empty",
        "truncated",
        "oversized",
        "bad-window",
        "bad-snapshot",
        "non-json",
        "query-failed",
        "exception",
    ],
)
def test_windows_recovery_capture_reports_unavailable_and_truncation(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    start = 1788739200000
    identity = "BrainsServeAll-evidence-test"
    payload = {
        "snapshot": {"state": 3, "last_task_result": 0, "last_run_time_utc_ms": start},
        "events": [],
        "events_available": True,
        "truncated": False,
        "window_end_ms": start + 2000,
    }
    if case == "disabled":
        payload["events_available"] = False
    elif case in {"truncated", "oversized"}:
        payload["events"] = [_task_event_xml(identity)] * (257 if case == "truncated" else 258)
    elif case == "bad-window":
        payload["window_end_ms"] = start + 600001
    elif case == "bad-snapshot":
        payload["snapshot"] = {"state": "secret", "command": "secret"}
    query = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            2 if case == "query-failed" else 0,
            "secret" if case == "non-json" else json.dumps(payload),
            "secret",
        )
    )
    if case == "exception":
        query.side_effect = subprocess.TimeoutExpired("secret", 30)
    monkeypatch.setattr(native_lifecycle, "_native_command", query)
    result = native_lifecycle._windows_recovery_capture("brains-serve-all-evidence-test", start)
    assert "secret" not in json.dumps(result)
    if case == "truncated":
        assert result["truncated"] is True and len(result["events"]) == 256
        assert result["source_count"] == result["matched_count"] == 257
    elif case in {"empty", "bad-snapshot"}:
        assert result["events_unavailable"] is False and result["source_count"] == 0
    else:
        assert result["events_unavailable"] is True
    if case == "bad-snapshot":
        assert result["snapshot"] is None


def test_windows_recovery_diagnostics_surround_unchanged_kill_and_wait() -> None:
    tree = ast.parse(_LIFECYCLE_PATH.read_text(encoding="utf-8"))
    prepare = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prepare"
    )
    capture = next(
        node
        for node in ast.walk(prepare)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_windows_recovery_capture"
    )
    guarded = next(
        node
        for node in ast.walk(prepare)
        if isinstance(node, ast.Try)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_kill_owned_tree"
            for call in ast.walk(node)
        )
    )
    calls = [
        node
        for node in ast.walk(guarded)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    kill = next(node for node in calls if node.func.id == "_kill_owned_tree")
    wait = next(node for node in calls if node.func.id == "_wait_healthy")
    emit = next(node for node in calls if node.func.id == "_emit_windows_recovery")
    assert capture.lineno < kill.lineno < wait.lineno < emit.lineno
    assert isinstance(kill.args[0], ast.Name) and kill.args[0].id == "old_pid"
    assert len(wait.args) == 2 and not wait.keywords
    assert any(node is emit for stmt in guarded.finalbody for node in ast.walk(stmt))
    assert not any(node.func.id in {"_run", "_write_plan", "_record"} for node in calls)


def test_native_config_removal_preserves_drift_links_and_unexpected_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    path = root / "config.toml"
    path.write_bytes(b"owned")
    monkeypatch.setattr(native_lifecycle, "_config_root", lambda _tool: root)
    snapshot = native_lifecycle._config_snapshot("codex")
    path.write_bytes(b"foreign")
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._remove_synthetic_config("codex", snapshot)
    assert path.read_bytes() == b"foreign"
    path.write_bytes(b"owned")
    unexpected = root / "unowned-empty"
    unexpected.mkdir()
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._remove_synthetic_config("codex", snapshot)
    assert unexpected.is_dir()
    assert path.read_bytes() == b"owned"
    path.write_bytes(b"owned")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path or original_is_symlink(self))
    with pytest.raises(native_lifecycle.EvidenceFailure):
        native_lifecycle._remove_synthetic_config("codex", snapshot)
    assert path.read_bytes() == b"owned"


@pytest.mark.parametrize("tool", ["opencode", "claude-code"])
@pytest.mark.parametrize("drift", [False, True])
def test_native_cleanup_accounts_real_wire_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, drift: bool
) -> None:
    from brains import wire

    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(native_lifecycle.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setattr(
        wire, "_opencode_compatibility", lambda: (wire.OPENCODE_SUPPORTED_VERSION, "synthetic")
    )
    # All file generation/removal below uses the real wire implementation. Only
    # external binary discovery/execution and permission hardening are mocked.
    monkeypatch.setattr(
        wire.subprocess, "run", Mock(side_effect=AssertionError("external execution"))
    )
    monkeypatch.setattr(wire, "_harden", lambda _path: None)
    clock = ["20260907-010101"]
    monkeypatch.setattr(wire, "_timestamp", lambda: clock[0])
    monkeypatch.setattr(native_lifecycle.time, "strftime", lambda *_args: clock[0])
    prior = home / ".claude.json.bak-20260901-010101"
    if tool == "claude-code":
        prior.write_bytes(b"preexisting backup")
    original = native_lifecycle._config_snapshot(tool)
    native_lifecycle._seed(native_lifecycle._config_path(tool), tool)
    baseline = native_lifecycle._config_snapshot(tool)
    native_lifecycle._check_backup_collision(tool)
    context = wire.WireContext(
        api_key="synthetic-credential",
        url="http://127.0.0.1:24002/mcp",
        db_url="sqlite:///" + (state / "brains.db").as_posix(),
    )
    report = wire.wire(home, context, tools=[tool], force=True, rules=False)
    assert report["ok"] is True
    wired = native_lifecycle._config_snapshot(tool)
    directories = native_lifecycle._config_directories(tool)
    if tool == "opencode":
        assert directories == [".", "plugins"]
        assert (home / ".config/opencode/plugins/brains-lifecycle.js").is_file()
    clock[0] = "20260907-010102"
    native_lifecycle._check_backup_collision(tool)
    report = wire.unwire(home, tools=[tool], rules=False)
    assert report["tools"][0]["mcp"]["action"] == "remove"
    restored = native_lifecycle._config_snapshot(tool)
    backups = native_evidence.account_managed_backups(baseline, wired, restored)
    assert len(backups) == 2
    if tool == "claude-code":
        secret_backup = home / ".claude.json.bak-20260907-010102"
        assert b"synthetic-credential" in secret_backup.read_bytes()
        assert prior.read_bytes() == b"preexisting backup"
    else:
        assert (home / ".config/opencode/plugins").is_dir()
        assert not (home / ".config/opencode/plugins/brains-lifecycle.js").exists()
    if drift:
        unexpected = (
            home / ".claude.json.bak-20260907-010103"
            if tool == "claude-code"
            else home / ".config/opencode/foreign"
        )
        if tool == "claude-code":
            unexpected.write_bytes(b"foreign backup")
        else:
            unexpected.mkdir()
        with pytest.raises(native_lifecycle.EvidenceFailure):
            native_lifecycle._remove_synthetic_config(
                tool, restored, directories=directories, original=original
            )
        assert unexpected.exists()
        assert native_lifecycle._config_path(tool).exists()
        return
    native_lifecycle._remove_synthetic_config(
        tool, restored, directories=directories, original=original
    )
    assert native_lifecycle._config_snapshot(tool) == original
    assert not native_lifecycle._config_root(tool).exists()
    if tool == "claude-code":
        assert list(home.glob(".claude.json.bak-*")) == [prior]
        clock[0] = "20260901-010101"
        with pytest.raises(native_lifecycle.EvidenceFailure, match="already exists"):
            native_lifecycle._check_backup_collision(tool)


@pytest.mark.parametrize("outcome", ["stopped", "timeout", "ownership-drift"])
def test_native_macos_stop_waits_for_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("BRAINS_NATIVE_EVIDENCE_ROOT", str(tmp_path / "runtime"))
    label = "brains-serve-all-evidence-11111111"
    plan = {"label": label, "executable": "synthetic"}
    polls: list[str] = []

    def ownership(_plan: dict) -> bool:
        polls.append("ownership")
        if outcome == "ownership-drift" and polls.count("ownership") == 2:
            raise native_lifecycle.EvidenceFailure("foreign definition")
        return True

    def status(_executable: str, _label: str) -> dict:
        polls.append("status")
        stopped = outcome == "stopped" and polls.count("status") == 2
        return {
            "platform": "macos",
            "label": native_lifecycle.native_service_identity("macos", label),
            "state": "not-loaded",
            "installed": False,
            "healthy": False,
            "runtime_classification": "stopped",
            "service_pid": {
                "pid": None if stopped else 123,
                "confidence": "absent" if stopped else "verified",
            },
            "listeners": {"gateway": not stopped, "mcp": not stopped},
            "mcp_protocol": {"ready": not stopped},
        }

    monkeypatch.setattr(native_lifecycle, "_assert_native_ownership", ownership)
    monkeypatch.setattr(native_lifecycle, "_status", status)
    ticks = iter((0, 0, 0.5, 1))
    monkeypatch.setattr(native_lifecycle.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _delay: None)
    if outcome == "stopped":
        evidence = native_lifecycle._wait_stopped(plan, timeout=1)
        assert evidence["owned_process"]["confidence"] == "absent"
        assert polls == ["ownership", "status", "ownership", "status"]
    else:
        with pytest.raises(native_lifecycle.EvidenceFailure):
            native_lifecycle._wait_stopped(plan, timeout=1)
        assert polls.count("status") == (1 if outcome == "ownership-drift" else 2)


@pytest.mark.parametrize("operation", ["_wait_healthy", "_wait_stopped", "_wait_removed"])
def test_native_timeout_diagnostics_include_only_bounded_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    monkeypatch.setattr(native_lifecycle.platform, "system", lambda: "Darwin")
    label = "brains-serve-all-evidence-11111111"
    secret = "synthetic-private-command-and-credential"
    report = {
        "platform": "macos",
        "label": native_lifecycle.native_service_identity("macos", label),
        "state": secret,
        "installed": False,
        "healthy": False,
        "listeners": {"gateway": True, "mcp": False},
        "mcp_protocol": {"ready": False, "detail": secret},
        "service_pid": {"pid": 123, "confidence": "verified", "reason": secret},
        "runtime_classification": secret,
        "detail": secret,
    }
    monkeypatch.setattr(native_lifecycle, "_status", lambda *_args: report)
    monkeypatch.setattr(native_lifecycle, "_assert_native_ownership", lambda _plan: True)
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(native_lifecycle.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(native_lifecycle.time, "sleep", lambda _delay: None)
    with pytest.raises(native_lifecycle.EvidenceFailure) as caught:
        if operation == "_wait_stopped":
            native_lifecycle._wait_stopped({"executable": str(tmp_path), "label": label}, timeout=1)
        else:
            getattr(native_lifecycle, operation)(str(tmp_path), label, timeout=1)
    native_lifecycle._diagnose(caught.value, phase="manager-cycle", stage="lifecycle")
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["operation"] == operation
    assert diagnostic["wait_status"] == {
        "installed": False,
        "healthy": False,
        "gateway_listening": True,
        "mcp_listening": False,
        "mcp_ready": False,
        "pid_present": True,
        "pid_verified": True,
        "pid_absent": False,
        "runtime_stopped": False,
    }
    assert secret not in json.dumps(diagnostic)
    assert str(tmp_path) not in json.dumps(diagnostic)
    assert "123" not in json.dumps(diagnostic)


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
        "native_definition": {},
        "runtime_owner": {},
        "runtime_inventory": {},
        "config_directories": [],
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
        assert provision["run"].strip().endswith("npm install --global opencode-ai@1.18.25")
        verifier = next(
            step for step in steps if "verify_native_evidence.py" in str(step.get("run", ""))
        )
        probe = next(
            step
            for step in steps
            if "probe_native_" in str(step.get("run", ""))
            and "--package-manifest" in str(step.get("run", ""))
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
    diagnostic = json.loads(result.stderr)
    assert diagnostic["error_code"] == "the-real-user-already-has-brains-state"
    assert diagnostic["stage"] == "guard"
    assert str(tmp_path) not in result.stderr
