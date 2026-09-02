"""Hermetic installation, wiring, and service lifecycle contract.

The native service managers are represented by stateful command fakes.  This
keeps the operator's services untouched while exercising the real backend
install/start/status/restart/stop/uninstall code and every supported wire
adapter against a clean synthetic home.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Protocol

import pytest

from brains import config as config_module
from brains import wire
from brains.service import linux, macos, windows
from brains.service.common import ServiceSpec


class ServiceBackend(Protocol):
    def install(self, spec: ServiceSpec, *, dry_run: bool = False) -> dict: ...

    def uninstall(self, *, dry_run: bool = False) -> dict: ...

    def start(self) -> dict: ...

    def stop(self) -> dict: ...

    def restart(self) -> dict: ...

    def status(self) -> dict: ...


class FakeServiceManager:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.installed = False
        self.running = False

    def __call__(self, command: list[str], **_kwargs: object) -> tuple[int, str, str]:
        joined = " ".join(command)
        if self.platform == "linux":
            if " enable --now " in f" {joined} ":
                self.installed = self.running = True
            elif " disable --now " in f" {joined} ":
                self.installed = self.running = False
            elif " is-enabled " in f" {joined} ":
                return (0, "enabled", "") if self.installed else (1, "", "disabled")
            elif " is-active " in f" {joined} ":
                return (0, "active", "") if self.running else (3, "inactive", "")
            elif " restart " in f" {joined} " or " start " in f" {joined} ":
                self.running = self.installed
            elif " stop " in f" {joined} ":
                self.running = False
            return 0, "ok", ""

        if self.platform == "macos":
            action = command[1]
            if action == "load":
                self.installed = self.running = True
            elif action == "unload":
                self.installed = self.running = False
            elif action == "start":
                self.running = self.installed
            elif action == "stop":
                self.running = False
            elif action == "list":
                return (0, "loaded", "") if self.installed else (1, "", "not loaded")
            return 0, "ok", ""

        action = command[1].lower()
        if action == "/create":
            self.installed = True
        elif action == "/run":
            self.running = self.installed
        elif action == "/end":
            self.running = False
        elif action == "/delete":
            self.installed = self.running = False
        elif action == "/query":
            if not self.installed:
                return 1, "", "not found"
            state = "Running" if self.running else "Ready"
            return 0, f'"\\BrainsServeAll","N/A","{state}"', ""
        return 0, "ok", ""


def _seed_unmanaged_config(home: Path, tool: str) -> Path:
    adapter = wire.ADAPTERS[tool]
    path = adapter.mcp_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if adapter.mcp_format == "toml":
        path.write_text('model = "synthetic-unmanaged"\n', encoding="utf-8")
    else:
        servers_key = adapter.json_servers_key
        path.write_text(
            json.dumps(
                {
                    "synthetic_unmanaged": True,
                    servers_key: {
                        "other": {"command": "synthetic-other-server"},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return path


def _assert_unmanaged_config_survived(path: Path, tool: str) -> None:
    adapter = wire.ADAPTERS[tool]
    text = path.read_text(encoding="utf-8")
    assert "brains:wire:start" not in text
    if adapter.mcp_format == "toml":
        assert 'model = "synthetic-unmanaged"' in text
    else:
        data = json.loads(text)
        assert data["synthetic_unmanaged"] is True
        assert data[adapter.json_servers_key]["other"]["command"] == "synthetic-other-server"
        assert "brains" not in data[adapter.json_servers_key]


@pytest.mark.parametrize(
    ("platform", "backend"),
    [("windows", windows), ("macos", macos), ("linux", linux)],
)
@pytest.mark.parametrize("tool", tuple(wire.ADAPTERS))
def test_clean_home_service_and_wire_lifecycle_is_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    backend: ServiceBackend,
    tool: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    for path in (home, state, workspace):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    config_module.reload_settings()

    manager = FakeServiceManager(platform)
    monkeypatch.setattr(backend, "run_cmd", manager)
    if platform in {"windows", "macos"}:
        monkeypatch.setattr(
            backend,
            "verify_pid",
            lambda _record: {"pid": None, "confidence": "absent", "reason": "synthetic"},
        )

    config_path = _seed_unmanaged_config(home, tool)
    context = wire.WireContext(
        transport="stdio",
        python=sys.executable,
        db_url=f"sqlite:///{(state / 'brains.sqlite').as_posix()}",
    )
    wired = wire.wire(home, context, tools=[tool], rules=False, force=True)
    assert wired["ok"] is True
    selected = next(row for row in wire.status(home)["tools"] if row["tool"] == tool)
    assert selected["mcp_wired"] is True
    assert selected["mcp_transport"] == "stdio"

    spec = ServiceSpec(
        program=sys.executable,
        args=("-m", "brains", "serve-all"),
        working_dir=str(workspace),
        user="synthetic-user",
    )
    installed = backend.install(spec)
    assert installed["ok"] is True
    assert backend.status()["installed"] is True
    marker = state / "synthetic-persistence-marker"
    marker.write_text("preserved", encoding="utf-8")
    assert backend.restart()["ok"] is True
    assert backend.status()["installed"] is True
    assert marker.read_text(encoding="utf-8") == "preserved"
    assert backend.stop()["ok"] is True

    unwired = wire.unwire(home, tools=[tool], rules=False)
    assert unwired["tools"][0]["mcp"]["action"] == "remove"
    _assert_unmanaged_config_survived(config_path, tool)
    removed = backend.uninstall()
    assert removed["ok"] is True
    assert backend.status()["installed"] is False
    assert marker.read_text(encoding="utf-8") == "preserved"

    monkeypatch.delenv("BRAINS_STATE_DIR", raising=False)
    config_module.reload_settings()
