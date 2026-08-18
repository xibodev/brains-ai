"""Tests for the freshness checker.

We monkeypatch the storage + event sinks (``write_freshness_check``,
``append_event``) to no-ops so each test exercises pure
``check_source`` branches without touching SQLite or emitting real
events. ``httpx.head`` is patched per test so we never hit the network.
We also stub ``socket.getaddrinfo`` so the SSRF safety check resolves
public hosts to a fixed public IP without performing real DNS.

Covers:
* HTTP source - happy path returns etag + last_modified
* HTTP source - allowlist rejection short-circuits before any network call
* HTTP source - transport error captured as ``ok=False`` + ``error``
* HTTP source - SSRF default-deny on loopback / private / link-local
* HTTP source - allowlist override permits an internal host
* HTTP source - redirect to private IP is blocked after the first hop
* HTTP source - too many redirects is captured as ``ok=False``
* Local file source - hash + mtime
* Git repo source - successful ``git rev-parse``
* Git repo source - subprocess failure recorded as ``ok=False``
"""

from __future__ import annotations

import os
import socket
import subprocess

import httpx
import pytest

from brains.context import freshness as fr


@pytest.fixture(autouse=True)
def _silence_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the side-effect functions so freshness tests stay hermetic."""
    monkeypatch.setattr(fr, "write_freshness_check", lambda *_a, **_kw: None)
    monkeypatch.setattr(fr, "append_event", lambda *_a, **_kw: None)


def _stub_public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    """Pretend every hostname resolves to a public IP (example.com's address
    by default). Tests can override per-host by patching directly."""
    monkeypatch.setattr(
        fr.socket,
        "getaddrinfo",
        lambda host, *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))],
    )


# --- HTTP source ---------------------------------------------------------


def test_http_source_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr.settings, "source_allowlist", [])
    _stub_public_dns(monkeypatch)

    fake_response = httpx.Response(
        200,
        headers={"etag": 'W/"abc"', "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
    )
    monkeypatch.setattr(fr.httpx, "head", lambda *_a, **_kw: fake_response)

    result = fr.check_source("https://example.com/spec.json")
    assert result["type"] == "web_url"
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["etag"] == 'W/"abc"'
    assert result["last_modified"] == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert result["redirect_chain"] is None


def test_http_source_blocked_by_allowlist_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fr.settings, "source_allowlist", ["only-this.example"])
    # If the allowlist check fails to short-circuit, this will raise.
    monkeypatch.setattr(
        fr.httpx,
        "head",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    result = fr.check_source("https://other.example/x")
    assert result["ok"] is False
    assert "allowlisted" in result["error"]
    assert result["type"] == "web_url"


def test_http_source_transport_error_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fr.settings, "source_allowlist", [])
    _stub_public_dns(monkeypatch)

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(fr.httpx, "head", _boom)

    result = fr.check_source("https://example.com/spec.json")
    assert result["ok"] is False
    assert "no route" in result["error"]


# --- SSRF default-deny ---------------------------------------------------


@pytest.mark.parametrize(
    "url, ip",
    [
        # Loopback
        ("http://127.0.0.1/x", "127.0.0.1"),
        ("http://localhost/x", "127.0.0.1"),
        # IPv6 loopback
        ("http://[::1]/x", "::1"),
        # AWS / GCP link-local metadata
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        # RFC1918 private
        ("http://10.0.0.5/x", "10.0.0.5"),
        ("http://192.168.1.10/x", "192.168.1.10"),
        ("http://172.16.0.1/x", "172.16.0.1"),
        # Unspecified
        ("http://0.0.0.0/x", "0.0.0.0"),
    ],
)
def test_http_source_internal_target_is_blocked(
    monkeypatch: pytest.MonkeyPatch, url: str, ip: str
) -> None:
    monkeypatch.setattr(fr.settings, "source_allowlist", [])
    # When the URL contains a raw IP, urlparse already exposes it as the
    # hostname and getaddrinfo won't be needed — but stub it anyway so a
    # hostname-style entry (localhost) also resolves deterministically.
    monkeypatch.setattr(
        fr.socket,
        "getaddrinfo",
        lambda host, *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))],
    )
    monkeypatch.setattr(
        fr.httpx,
        "head",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    result = fr.check_source(url)
    assert result["ok"] is False
    assert "internal" in result["error"]


def test_http_source_allowlist_override_permits_internal_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-provided allowlist entry bypasses the IP-class check so
    legitimate internal documentation servers stay reachable."""
    monkeypatch.setattr(fr.settings, "source_allowlist", ["docs.internal"])
    monkeypatch.setattr(
        fr.socket,
        "getaddrinfo",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("DNS should be bypassed for explicit allowlist")
        ),
    )
    monkeypatch.setattr(fr.httpx, "head", lambda *_a, **_kw: httpx.Response(200, headers={}))

    result = fr.check_source("https://docs.internal/spec.json")
    assert result["ok"] is True
    assert result["status"] == 200


def test_http_source_redirect_to_private_ip_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public URL that 302s into RFC1918 must be refused at the second
    hop so an attacker can't smuggle SSRF via redirect."""
    monkeypatch.setattr(fr.settings, "source_allowlist", [])
    resolved: dict[str, str] = {
        "public.example.com": "93.184.216.34",
        "10.0.0.5": "10.0.0.5",
    }
    monkeypatch.setattr(
        fr.socket,
        "getaddrinfo",
        lambda host, *_a, **_kw: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (resolved.get(host, "1.1.1.1"), 0),
            )
        ],
    )

    calls: list[str] = []

    def _head(url, **_kw):
        calls.append(url)
        if url.startswith("https://public.example.com"):
            return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})
        raise AssertionError(f"unexpected second-hop call: {url}")

    monkeypatch.setattr(fr.httpx, "head", _head)
    result = fr.check_source("https://public.example.com/x")
    assert result["ok"] is False
    assert "internal" in result["error"]
    assert result["redirect_chain"] == ["https://public.example.com/x"]
    # First hop was made, second hop was blocked before any network call.
    assert calls == ["https://public.example.com/x"]


def test_http_source_too_many_redirects_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fr.settings, "source_allowlist", [])
    _stub_public_dns(monkeypatch)
    counter = {"n": 0}

    def _head(url, **_kw):
        counter["n"] += 1
        # Always redirect to a new public URL — exceeds the cap.
        return httpx.Response(302, headers={"location": f"https://example.com/next-{counter['n']}"})

    monkeypatch.setattr(fr.httpx, "head", _head)
    result = fr.check_source("https://example.com/start")
    assert result["ok"] is False
    assert "too many redirects" in result["error"]
    assert len(result["redirect_chain"]) == fr._MAX_REDIRECTS + 1


# --- Local file source ---------------------------------------------------


def test_local_file_source_returns_sha256_and_mtime(tmp_path) -> None:
    path = tmp_path / "spec.txt"
    path.write_bytes(b"hello world")
    result = fr.check_source(str(path))
    assert result["type"] == "local_file"
    assert result["ok"] is True
    assert result["hash"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert isinstance(result["mtime"], float)


# --- Git repo source -----------------------------------------------------


def test_git_repo_source_records_commit_branch_and_remote(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    def _fake_check_output(cmd, **_kw):
        if "rev-parse" in cmd and "HEAD" in cmd and "--abbrev-ref" not in cmd:
            return "abc123\n"
        if "--abbrev-ref" in cmd:
            return "main\n"
        if "remote" in cmd and "get-url" in cmd:
            return "git@github.com:example/repo.git\n"
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(subprocess, "check_output", _fake_check_output)
    result = fr.check_source(str(repo_dir))
    assert result["type"] == "git_repo"
    assert result["ok"] is True
    assert result["commit"] == "abc123"
    assert result["branch"] == "main"
    assert result["remote"] == "git@github.com:example/repo.git"


def test_git_repo_source_failure_is_recorded_not_raised(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "not-a-git-repo"
    repo_dir.mkdir()

    def _fail(*_a, **_kw):
        raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(subprocess, "check_output", _fail)
    result = fr.check_source(str(repo_dir))
    assert result["type"] == "git_repo"
    assert result["ok"] is False
    assert "git metadata unavailable" in result["error"]


def test_nonexistent_path_falls_into_git_branch_with_ok_true() -> None:
    """A source that isn't HTTP, isn't a file, and isn't a real dir is
    treated as a (possibly remote) git repo and returns ok=True with no
    extra metadata. This documents the current behavior so any refactor
    of the source-type detection breaks loudly here first."""
    result = fr.check_source("/definitely/does/not/exist/" + os.urandom(4).hex())
    assert result["type"] == "git_repo"
    assert result["ok"] is True
