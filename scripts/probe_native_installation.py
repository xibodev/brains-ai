"""Clean-home package and wiring probe for native CI runners.

This intentionally uses ``service install --dry-run``.  It proves that the
installed wheel resolves the native backend and renders its definition, but it
does not claim that Task Scheduler, launchd, or systemd executed the service.
The real scheduler lifecycle remains a BL-P0-06 evidence requirement.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TOOLS = ("copilot-cli", "claude-code", "codex", "opencode")


def _run(executable: str, env: dict[str, str], *args: str) -> dict:
    completed = subprocess.run(
        [executable, *args],
        check=True,
        capture_output=True,
        env=env,
        text=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


def _config_path(home: Path, tool: str) -> Path:
    return {
        "copilot-cli": home / ".copilot" / "mcp-config.json",
        "claude-code": home / ".claude.json",
        "codex": home / ".codex" / "config.toml",
        "opencode": home / ".config" / "opencode" / "opencode.json",
    }[tool]


def _seed(path: Path, tool: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if tool == "codex":
        path.write_text('model = "synthetic-native-probe"\n', encoding="utf-8")
        return
    servers_key = "mcp" if tool == "opencode" else "mcpServers"
    path.write_text(
        json.dumps(
            {
                "synthetic_unmanaged": True,
                servers_key: {"other": {"command": "synthetic-other-server"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    executable = shutil.which("brains-ai")
    if executable is None:
        raise RuntimeError("the installed brains-ai console script is unavailable")
    with tempfile.TemporaryDirectory(prefix="brains-native-install-") as raw:
        root = Path(raw)
        home = root / "home"
        state = root / "state"
        workspace = root / "workspace"
        for path in (home, state, workspace):
            path.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "BRAINS_STATE_DIR": str(state),
                "BRAINS_API_KEY": "synthetic-native-probe-key",
                "BRAINS_MCP_BEARER_TOKEN": "synthetic-native-probe-key",
            }
        )

        first = _run(executable, env, "setup", "--path", str(workspace), "--no-wire", "--json")
        first_workspace = next(row for row in first["steps"] if row["step"] == "init")["workspace"][
            "slug"
        ]
        rendered = _run(executable, env, "service", "install", "--dry-run")
        assert rendered["action"] == "would-install"
        assert rendered["platform"] in {"windows", "macos", "linux"}

        for tool in TOOLS:
            path = _config_path(home, tool)
            _seed(path, tool)
            wired = _run(
                executable,
                env,
                "wire",
                "--tool",
                tool,
                "--force",
                "--no-rules",
                "--transport",
                "streamable-http",
            )
            assert wired["ok"] is True
            status = _run(executable, env, "wire", "--status")
            selected = next(row for row in status["tools"] if row["tool"] == tool)
            assert selected["mcp_wired"] is True
            assert selected["mcp_transport"] == "streamable-http"
            _run(executable, env, "unwire", "--tool", tool, "--no-rules")
            after = _run(executable, env, "wire", "--status")
            removed = next(row for row in after["tools"] if row["tool"] == tool)
            assert removed["mcp_wired"] is False
            residual = path.read_text(encoding="utf-8")
            assert "synthetic" in residual
            assert "brains:wire:start" not in residual

        second = _run(executable, env, "setup", "--path", str(workspace), "--no-wire", "--json")
        second_workspace = next(row for row in second["steps"] if row["step"] == "init")[
            "workspace"
        ]["slug"]
        assert second_workspace == first_workspace
    print("native package, clean-home, rendering, and wire rollback probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
