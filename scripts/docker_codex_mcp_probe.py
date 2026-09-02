"""Provider-free Codex CLI acceptance for the managed Brains MCP entry.

This probe deliberately stops at ``codex mcp get``: invoking an agent turn would
require provider credentials. The same Docker acceptance run separately performs
the wire-protocol proof with the real MCP SDK (initialize plus tools/list).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

CODEX_PACKAGE = "@openai/codex@0.152.1"
API_KEY = "synthetic-docker-codex-key"
BEARER_ENV = "BRAINS_MCP_BEARER_TOKEN"


def _run_wire(home: Path, client_token: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "BRAINS_STATE_DIR": str(home / ".brains"),
            "BRAINS_DB_URL": f"sqlite:///{(home / '.brains/brains.sqlite').as_posix()}",
            "BRAINS_API_KEY": API_KEY,
        }
    )
    if client_token is None:
        env.pop(BEARER_ENV, None)
    else:
        env[BEARER_ENV] = client_token
    (home / ".codex").mkdir(parents=True)
    return subprocess.run(
        ["brains-ai", "wire", "--tool", "codex", "--no-rules"],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_preflight_refuses(client_token: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="brains-codex-refusal-") as raw_home:
        home = Path(raw_home)
        result = _run_wire(home, client_token)
        assert result.returncode != 0, "unsafe Codex wiring unexpectedly succeeded"
        report = json.loads(result.stdout)
        assert report["ok"] is False
        assert len(report["tools"]) == 1
        mcp = report["tools"][0]["mcp"]
        assert report["tools"][0]["tool"] == "codex"
        assert mcp["action"] == "error"
        assert mcp["bearer_token_env_var"] == BEARER_ENV
        expected_reason = "is unavailable" if client_token is None else "does not match"
        assert expected_reason in mcp["detail"]
        assert not (home / ".codex/config.toml").exists(), (
            "Codex authentication preflight failed after writing configuration"
        )


def main() -> None:
    _assert_preflight_refuses(None)
    _assert_preflight_refuses("synthetic-mismatched-client-key")

    with tempfile.TemporaryDirectory(prefix="brains-codex-accepted-") as raw_home:
        home = Path(raw_home)
        result = _run_wire(home, API_KEY)
        assert result.returncode == 0, result.stderr
        config = home / ".codex/config.toml"
        assert config.is_file()

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                BEARER_ENV: API_KEY,
                "NPM_CONFIG_CACHE": str(home / ".npm-cache"),
            }
        )
        version = subprocess.run(
            ["npx", "--yes", CODEX_PACKAGE, "--version"],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert version.returncode == 0, version.stderr
        assert "0.152.1" in version.stdout

        parsed = subprocess.run(
            ["npx", "--yes", CODEX_PACKAGE, "mcp", "get", "brains"],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert parsed.returncode == 0, parsed.stderr
        output = parsed.stdout.casefold().replace("-", "_")
        assert "streamable_http" in output
        assert "http://127.0.0.1:9877/mcp" in output
        assert BEARER_ENV.casefold() in output

    print(
        "Codex 0.152.1 parsed the isolated managed streamable_http entry; "
        "provider-free boundary retained"
    )


if __name__ == "__main__":
    main()
