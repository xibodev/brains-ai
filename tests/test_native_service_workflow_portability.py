from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def installation_step() -> dict:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load(
        (root / ".github/workflows/native-service-evidence.yml").read_text(encoding="utf-8")
    )
    return next(
        step
        for step in workflow["jobs"]["manager-cycle"]["steps"]
        if step.get("name")
        == "Build and install the exact candidate in a fresh private environment"
    )


def test_native_service_workflow_uses_portable_paths_and_digest(installation_step: dict) -> None:
    script = installation_step["run"]
    assert installation_step["shell"] == "bash"
    assert installation_step["env"]["EXPECTED_MANIFEST_SHA256"] == (
        "${{ needs.package.outputs.manifest-sha256 }}"
    )
    assert 'temp_dir="$(cygpath -u "$RUNNER_TEMP")"' in script
    assert 'temp_dir="$RUNNER_TEMP"' in script
    assert script.count('find "$temp_dir/native-service-dist"') == 2
    assert 'find "$RUNNER_TEMP/' not in script
    assert "sha256sum" not in script
    for name in ("artifact", "runtime", "venv"):
        assert f'{name}="$temp_dir/native-service-{name}-' in script
    assert 'test "$wheel_count" = 1' in script
    assert "BRAINS_NATIVE_PACKAGE_MANIFEST=$temp_dir/native-service-dist/" in script
    assert script.index('runtime="$(cygpath -w "$runtime")"') < script.index(
        'echo "BRAINS_NATIVE_EVIDENCE_ROOT=$runtime"'
    )
    assert 'echo "BRAINS_STATE_DIR=$runtime/state"' in script
    assert script.index("python - <<'PY'") < script.index('python -m venv "$venv"')


@pytest.mark.parametrize("case", ("matching", "mismatched", "missing"))
def test_native_service_workflow_checks_manifest_bytes(
    installation_step: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    dist = tmp_path / "native-service-dist"
    dist.mkdir()
    content = b'{"synthetic": true}\n'
    if case != "missing":
        (dist / "native-wakeup-package-provenance.json").write_bytes(content)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv(
        "EXPECTED_MANIFEST_SHA256",
        "0" * 64 if case == "mismatched" else hashlib.sha256(content).hexdigest(),
    )
    source = installation_step["run"].split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    code = compile(source, "native-service-workflow-manifest", "exec")
    if case == "matching":
        exec(code, {})
    elif case == "mismatched":
        with pytest.raises(SystemExit, match="candidate package provenance does not match"):
            exec(code, {})
    else:
        with pytest.raises(FileNotFoundError):
            exec(code, {})
