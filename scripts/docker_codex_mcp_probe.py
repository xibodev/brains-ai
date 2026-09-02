"""Prove Codex can start the managed Brains MCP entry without personal credentials.

All processes use disposable state and synthetic credentials. The agent turn is
sent to a local provider that deliberately returns 503, separating a successful
MCP startup handshake from the intentional model-provider failure.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODEX_PACKAGE = "@openai/codex@0.152.1"
API_KEY = "synthetic-docker-codex-key"
BEARER_ENV = "BRAINS_MCP_BEARER_TOKEN"
PROVIDER_KEY_ENV = "BRAINS_E2E_MOCK_PROVIDER_KEY"


def _run_wire(
    home: Path, client_token: str | None, *, url: str | None = None
) -> subprocess.CompletedProcess[str]:
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
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    command = ["brains-ai", "wire", "--tool", "codex", "--no-rules"]
    if url is not None:
        command.extend(["--url", url])
    return subprocess.run(
        command,
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


class _FailingProviderHandler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        type(self).requests.append(self.path)
        # Codex starts configured MCP clients concurrently. Keep the intentional
        # provider failure pending long enough for the startup handshake to finish.
        time.sleep(10)
        payload = json.dumps(
            {
                "error": {
                    "message": "intentional isolated provider failure",
                    "type": "mock_failure",
                }
            }
        ).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        del args


@contextmanager
def _failing_provider() -> Iterator[tuple[str, list[str]]]:
    _FailingProviderHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailingProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/v1", _FailingProviderHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "mock provider did not stop"


def _unused_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _brains_mcp_server(home: Path) -> Iterator[tuple[str, Path]]:
    port = _unused_loopback_port()
    log_path = home / "brains-mcp-access.log"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "BRAINS_STATE_DIR": str(home / ".brains"),
            "BRAINS_DB_URL": f"sqlite:///{(home / '.brains/brains.sqlite').as_posix()}",
            "BRAINS_API_KEY": API_KEY,
            "BRAINS_PREWARM_INDEX_ON_SESSION": "0",
        }
    )
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                "brains-ai",
                "mcp",
                "--mode",
                "streamable-http",
                "--port",
                str(port),
            ],
            cwd=home,
            env=env,
            stdout=log,
            stderr=log,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        "isolated Brains MCP server exited before accepting connections"
                    )
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("isolated Brains MCP server did not become reachable")
            yield f"http://127.0.0.1:{port}/mcp", log_path
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _seed_mock_provider_config(home: Path, base_url: str) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model = "mock-model"',
                'model_provider = "brains_e2e_mock"',
                'approval_policy = "never"',
                'sandbox_mode = "read-only"',
                "check_for_update_on_startup = false",
                "",
                "[model_providers.brains_e2e_mock]",
                'name = "Isolated intentional failure"',
                f'base_url = "{base_url}"',
                f'env_key = "{PROVIDER_KEY_ENV}"',
                'wire_api = "responses"',
                "request_max_retries = 0",
                "stream_max_retries = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> None:
    _assert_preflight_refuses(None)
    _assert_preflight_refuses("synthetic-mismatched-client-key")

    with tempfile.TemporaryDirectory(prefix="brains-codex-accepted-") as raw_home:
        home = Path(raw_home)
        with _failing_provider() as (provider_url, provider_requests):
            _seed_mock_provider_config(home, provider_url)
            with _brains_mcp_server(home) as (mcp_url, access_log_path):
                result = _run_wire(home, API_KEY, url=mcp_url)
                assert result.returncode == 0, result.stderr
                config = home / ".codex/config.toml"
                assert config.is_file()

                env = os.environ.copy()
                env.update(
                    {
                        "HOME": str(home),
                        "CODEX_HOME": str(home / ".codex"),
                        BEARER_ENV: API_KEY,
                        PROVIDER_KEY_ENV: "synthetic-isolated-provider-key",
                        "NPM_CONFIG_CACHE": str(home / ".npm-cache"),
                        "RUST_LOG": "codex_core::mcp=debug,codex_mcp_client=debug",
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
                assert mcp_url in output
                assert BEARER_ENV.casefold() in output

                agent = subprocess.run(
                    [
                        "npx",
                        "--yes",
                        CODEX_PACKAGE,
                        "exec",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        "--json",
                        "Return one short word.",
                    ],
                    cwd=home,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert agent.returncode != 0, "intentional provider failure was not observed"
                agent_output = f"{agent.stdout}\n{agent.stderr}".casefold()
                assert provider_requests, "Codex did not call the isolated mock provider"
                assert provider_requests[0].endswith("/responses")
                assert (
                    "intentional isolated provider failure" in agent_output or "503" in agent_output
                )
                forbidden_mcp_errors = (
                    "mcp client for `brains` failed to start",
                    "mcp startup failed",
                    "failed to initialize mcp",
                )
                assert not any(message in agent_output for message in forbidden_mcp_errors), (
                    agent_output
                )

            access_log = access_log_path.read_text(encoding="utf-8")
            successful_posts = [
                line for line in access_log.splitlines() if "POST /mcp" in line and "200 OK" in line
            ]
            assert successful_posts, "Codex did not complete an authenticated MCP request"
            assert not any(
                "POST /mcp" in line and "401 Unauthorized" in line
                for line in access_log.splitlines()
            )

    print(
        "Codex 0.152.1 parsed the managed entry and completed authenticated MCP startup; "
        "intentional isolated provider failure observed"
    )


if __name__ == "__main__":
    main()
