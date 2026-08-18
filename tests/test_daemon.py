"""Tests for the brains daemon (detection, config, register payload, the
poll→claim→spawn cycle in ``--once`` mode, and GC sweep).

The spawn path is MOCKED — ``brains.exec.runner.run_session`` is patched so no
real CLI is launched (shims are POSIX-only; these tests must pass on Windows +
Linux). The hub side is the real FastAPI app, reached through an in-process
``httpx.ASGITransport`` so the daemon exercises the genuine wire protocol.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from brains.control import issues, orgs, personas, projects, runtimes
from brains.control.operators import ensure_admin_operator
from brains.daemon import Daemon
from brains.daemon.client import HubClient
from brains.daemon.config import DaemonConfig, ToolOverride, load_config
from brains.daemon.detect import detect_tools
from brains.main import app


def test_daemon_consumes_only_its_own_stop_request(tmp_path, monkeypatch):
    import os

    from brains.daemon import daemon as daemon_module

    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    stopfile = tmp_path / "daemon.stop"
    stopfile.write_text(str(os.getpid() + 1), encoding="utf-8")
    assert daemon_module._consume_stop_request() is False
    assert stopfile.exists()
    stopfile.write_text(str(os.getpid()), encoding="utf-8")
    assert daemon_module._consume_stop_request() is True
    assert not stopfile.exists()


@pytest.fixture(autouse=True)
def _bootstrap():
    from brains.storage.migrations import init_db

    init_db()
    ensure_admin_operator()
    yield


def _hub_client(machine_id: str) -> HubClient:
    # The FastAPI TestClient is a *sync* httpx.Client bound to the app — exactly
    # what HubClient needs (ASGITransport is async-only).
    return HubClient("http://testserver", "local-dev-key", http=TestClient(app))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_detect_returns_known_tool_shape():
    cfg = DaemonConfig()
    with (
        patch("brains.daemon.detect.shutil.which", return_value="/usr/bin/copilot"),
        patch("brains.daemon.detect._probe_version", return_value="copilot 1.0.0"),
    ):
        found = detect_tools(cfg)
    tools = {f["tool"]: f for f in found}
    assert "copilot" in tools
    entry = tools["copilot"]
    assert entry["binary"] == "/usr/bin/copilot"
    assert entry["version"] == "copilot 1.0.0"
    assert entry["capabilities"]["tool"] == "copilot"
    assert "models" in entry["capabilities"]


def test_detect_skips_disabled_tool():
    cfg = DaemonConfig(tools={"copilot": ToolOverride(enabled=False)})
    with (
        patch("brains.daemon.detect.shutil.which", return_value="/usr/bin/copilot"),
        patch("brains.daemon.detect._probe_version", return_value="1.0"),
    ):
        found = detect_tools(cfg)
    assert all(f["tool"] != "copilot" for f in found)


def test_detect_honors_path_override():
    cfg = DaemonConfig(tools={"copilot": ToolOverride(path="/opt/copilot/bin/copilot")})
    with (
        patch("brains.daemon.detect.shutil.which", return_value=None),
        patch("brains.daemon.detect._probe_version", return_value="1.0"),
    ):
        found = {f["tool"]: f for f in detect_tools(cfg)}
    assert found["copilot"]["binary"] == "/opt/copilot/bin/copilot"


# --------------------------------------------------------------------------- #
# Enrolment (Connect a machine, F1) — daemon redeems WITHOUT an operator key
# --------------------------------------------------------------------------- #


def test_daemon_enrol_once_registers_runtimes_without_key():
    from brains.control import enrolment as enrolment_ctl

    machine_id = f"box-{uuid.uuid4().hex[:8]}"
    minted = enrolment_ctl.mint_token(label="laptop", ttl_seconds=900)

    # A keyless HubClient — proves the redeem path needs no operator credential.
    cfg = DaemonConfig(machine_id=machine_id)
    daemon = Daemon(cfg, client=HubClient("http://testserver", "", http=TestClient(app)))

    with patch(
        "brains.daemon.daemon.detect_tools",
        return_value=[
            {"tool": "copilot", "version": "1.0.65"},
            {"tool": "claude", "version": "2.0.1"},
        ],
    ):
        resp = daemon.enrol_once(minted["token"])

    tools = {r["tool"] for r in resp["runtimes"]}
    assert {"copilot", "claude"} <= tools
    # The machine's runtimes are now visible on the hub.
    listed = {r["tool"] for r in runtimes.list_runtimes(machine_id=machine_id)}
    assert {"copilot", "claude"} <= listed


def test_enrol_returns_daemon_key_that_authenticates_ongoing_loop():
    """ASK-0008 (A): the redeem mints a narrow daemon key (NOT the admin key) that
    the daemon uses for its ONGOING loop. A HubClient carrying ONLY that key can
    heartbeat — proving a token-enrolled machine stays live without an admin key."""
    from brains.control import enrolment as enrolment_ctl

    machine_id = f"box-{uuid.uuid4().hex[:8]}"
    minted = enrolment_ctl.mint_token(label="laptop", ttl_seconds=900)

    daemon = Daemon(
        DaemonConfig(machine_id=machine_id),
        client=HubClient("http://testserver", "", http=TestClient(app)),
    )
    with patch(
        "brains.daemon.daemon.detect_tools",
        return_value=[{"tool": "copilot", "version": "1.0.65"}],
    ):
        resp = daemon.enrol_once(minted["token"])

    daemon_key = resp["daemon_key"]
    assert daemon_key, "redeem did not return a daemon key"
    assert daemon_key != "local-dev-key", "must NOT hand back the admin key"
    # enrol_once adopts the key for the ongoing loop.
    assert daemon.config.operator_key == daemon_key

    # A client carrying ONLY the daemon key can do the authed ongoing loop.
    keyed = HubClient("http://testserver", daemon_key, http=TestClient(app))
    rts = keyed.list_runtimes(machine_id=machine_id)
    assert rts, "daemon key could not read its runtimes"
    hb = keyed.heartbeat_batch(machine_id, [{"id": rts[0]["id"], "status": "online"}])
    assert hb["runtimes"], "daemon key could not heartbeat"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_load_config_env_and_flag_precedence(monkeypatch):
    monkeypatch.setenv("BRAINS_DAEMON_HUB_URL", "http://env-hub:9000")
    monkeypatch.setenv("BRAINS_DAEMON_MAX_CONCURRENT", "7")
    # Flag (explicit override) beats env.
    cfg = load_config(hub_url="http://flag-hub:1234")
    assert cfg.hub_url == "http://flag-hub:1234"
    assert cfg.max_concurrent == 7
    assert cfg.machine_id  # derived from current_machine_id()


def test_load_config_per_tool_env_override(monkeypatch):
    monkeypatch.setenv("BRAINS_DAEMON_TOOL_COPILOT_MODEL", "gpt-5.4")
    monkeypatch.setenv("BRAINS_DAEMON_TOOL_COPILOT_ENABLED", "false")
    cfg = load_config()
    ov = cfg.tool_override("copilot")
    assert ov.model == "gpt-5.4"
    assert ov.enabled is False


# --------------------------------------------------------------------------- #
# Register payload shape
# --------------------------------------------------------------------------- #


def test_build_register_payload_shape():
    cfg = DaemonConfig(machine_id="m-123", machine_label="box", working_root="/work")
    daemon = Daemon(cfg, client=MagicMock())
    detected = [
        {
            "tool": "copilot",
            "display_name": "Copilot CLI",
            "binary": "/b/copilot",
            "version": "1.0",
            "capabilities": {"tool": "copilot"},
        },
    ]
    payload = daemon.build_register_payload(detected)
    assert payload["machine_id"] == "m-123"
    assert payload["working_root"] == "/work"
    assert payload["os"] in {"linux", "darwin", "win32"}
    assert payload["tools"][0]["tool"] == "copilot"
    assert payload["tools"][0]["capabilities"] == {"tool": "copilot"}


# --------------------------------------------------------------------------- #
# Poll → claim → spawn cycle (--once)
# --------------------------------------------------------------------------- #


@pytest.fixture
def assigned(tmp_path):
    """Register a runtime for a known machine and queue one assignment to it."""
    machine = f"daemon-machine-{uuid.uuid4().hex[:8]}"
    org = orgs.create_org(f"org-{uuid.uuid4().hex[:8]}", "Acme")
    rt = runtimes.register_runtime(
        machine, "copilot", org_id=org["id"], working_root=str(tmp_path), status="online"
    )
    persona = personas.create_persona(
        org["id"],
        f"forge-{uuid.uuid4().hex[:6]}",
        "Forge",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=rt["id"],
    )
    proj = projects.create_project(org["id"], f"proj-{uuid.uuid4().hex[:6]}", "Proj")
    issue = issues.create_issue(proj["id"], "Do the work", body="please")
    issues.assign(issue["code"], persona_id=persona["id"])
    return machine, rt, persona, issue


def test_run_once_claims_and_spawns(assigned, tmp_path):
    machine, rt, _persona, issue = assigned
    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))

    fake = MagicMock(return_value={"returncode": 0, "session_id": "ses_runner"})
    with patch("brains.exec.runner.run_session", fake):
        result = daemon.run_once(detect=False)

    # The spawn went through the (mocked) gated runner exactly once.
    assert fake.call_count == 1
    acted = result["acted"]
    assert len(acted) == 1
    assert acted[0]["issue_id"] == issue["id"]
    assert acted[0]["state"] == "finished"
    # Issue advanced open → in_progress (claim) → in_review (ack finished).
    assert issues.get_issue(issue["id"])["status"] == "in_review"


def test_run_once_failed_spawn_aborts_issue(assigned, tmp_path):
    machine, _rt, _persona, issue = assigned
    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))

    fake = MagicMock(return_value={"returncode": 1, "session_id": "ses_runner"})
    with patch("brains.exec.runner.run_session", fake):
        daemon.run_once(detect=False)

    # A non-zero return reopens the issue for another runtime to re-claim.
    assert issues.get_issue(issue["id"])["status"] == "open"


def test_run_once_respects_max_concurrent(assigned, tmp_path):
    machine, rt, persona, _issue = assigned
    # Queue a second assignment to the same runtime.
    proj = projects.create_project(
        _persona_org_id := personas.get_persona(persona["id"])["org_id"],
        f"proj2-{uuid.uuid4().hex[:6]}",
        "Proj2",
    )
    issue2 = issues.create_issue(proj["id"], "Second")
    issues.assign(issue2["code"], persona_id=persona["id"])

    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path), max_concurrent=1)
    daemon = Daemon(cfg, client=_hub_client(machine))
    fake = MagicMock(return_value={"returncode": 0, "session_id": "ses_x"})
    with patch("brains.exec.runner.run_session", fake):
        result = daemon.run_once(detect=False)
    # Cap of 1 → only one assignment spawned this cycle.
    assert fake.call_count == 1
    assert len(result["acted"]) == 1


def test_run_once_no_assignments_is_noop(tmp_path):
    machine = f"idle-{uuid.uuid4().hex[:8]}"
    runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path), status="online")
    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))
    fake = MagicMock()
    with patch("brains.exec.runner.run_session", fake):
        result = daemon.run_once(detect=False)
    assert fake.call_count == 0
    assert result["acted"] == []


def test_run_once_skips_an_org_less_runtime(tmp_path):
    """A pre-Org Runtime owns no Org, so no Org's work may be run through it."""
    machine = f"legacy-{uuid.uuid4().hex[:8]}"
    rt = runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path), status="online")
    assert rt["org_id"] is None
    org = orgs.create_org(f"org-{uuid.uuid4().hex[:8]}", "Acme")
    persona = personas.create_persona(
        org["id"],
        f"legacy-{uuid.uuid4().hex[:6]}",
        "Legacy",
        model="claude-opus-4.8",
        tool="copilot",
        default_runtime_id=rt["id"],
    )
    proj = projects.create_project(org["id"], f"proj-{uuid.uuid4().hex[:6]}", "Proj")
    issue = issues.create_issue(proj["id"], "Do the work")
    issues.assign(issue["code"], persona_id=persona["id"])

    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))
    fake = MagicMock(return_value={"returncode": 0, "session_id": "ses_legacy"})
    with patch("brains.exec.runner.run_session", fake):
        result = daemon.run_once(detect=False)

    assert fake.call_count == 0
    assert result["acted"] == []
    # The assignment is untouched, so a claimed Runtime can still pick it up.
    assert issues.get_issue(issue["id"])["status"] == "open"


# --------------------------------------------------------------------------- #
# Heartbeat + GC
# --------------------------------------------------------------------------- #


def test_heartbeat_once_updates_liveness(tmp_path):
    machine = f"hb-{uuid.uuid4().hex[:8]}"
    rt = runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path))
    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))
    out = daemon.heartbeat_once(status="online")
    assert out["runtimes"][0]["id"] == rt["id"]
    assert out["runtimes"][0]["status"] == "online"


def test_sweep_flips_stale_runtime_offline(tmp_path):
    machine = f"gc-{uuid.uuid4().hex[:8]}"
    rt = runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path))
    assert rt["status"] == "online"
    flipped = runtimes.sweep_stale(ttl_seconds=-1)
    assert rt["slug"] in {f["slug"] for f in flipped}
    assert runtimes.get_runtime(rt["id"])["status"] == "offline"


def test_drain_sets_runtimes_draining(tmp_path):
    machine = f"drain-{uuid.uuid4().hex[:8]}"
    rt = runtimes.register_runtime(machine, "copilot", working_root=str(tmp_path))
    cfg = DaemonConfig(machine_id=machine, working_root=str(tmp_path))
    daemon = Daemon(cfg, client=_hub_client(machine))
    daemon.drain()
    assert runtimes.get_runtime(rt["id"])["status"] == "draining"
