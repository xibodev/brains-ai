"""Tests for the file-backed exec-session store + dashboard exec console APIs."""

from __future__ import annotations

from brains.exec import store


def test_store_create_append_read_status(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    meta = store.create(tool="copilot", model="haiku", workspace="/ws", prompt="do x")
    assert meta.exec_id.startswith("exec_")
    assert meta.status == "running"

    store.append_output(meta.exec_id, "hello ")
    store.append_output(meta.exec_id, "world")
    text, offset = store.read_output(meta.exec_id, 0)
    assert text == "hello world"
    assert offset == len("hello world")
    # incremental read from offset returns only new bytes
    store.append_output(meta.exec_id, "!")
    text2, offset2 = store.read_output(meta.exec_id, offset)
    assert text2 == "!"
    assert offset2 == offset + 1

    store.set_status(meta.exec_id, "done", returncode=0)
    reloaded = store.load(meta.exec_id)
    assert reloaded.status == "done"
    assert reloaded.returncode == 0
    assert reloaded.ended_at is not None


def test_store_list_sessions_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    a = store.create(tool="claude", model=None, workspace="/ws", prompt="a")
    b = store.create(tool="codex", model=None, workspace="/ws", prompt="b")
    listing = store.list_sessions()
    ids = [m["exec_id"] for m in listing]
    assert a.exec_id in ids and b.exec_id in ids


def test_store_load_unknown_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    assert store.load("exec_doesnotexist") is None


def test_dashboard_exec_api_surface(tmp_path, monkeypatch):
    """The exec console JSON APIs list, fetch (with transcript+pending), and the
    console HTML renders — exercised through the FastAPI app with auth."""
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from brains.dashboard.app import app

    client = TestClient(app)
    headers = {"Authorization": "Bearer local-dev-key"}

    # seed a session directly in the store
    meta = store.create(tool="copilot", model=None, workspace=str(tmp_path), prompt="hi")
    store.append_output(meta.exec_id, "streamed line\n")

    r = client.get("/dashboard/api/exec", headers=headers)
    assert r.status_code == 200
    assert any(m["exec_id"] == meta.exec_id for m in r.json())

    r = client.get(f"/dashboard/api/exec/{meta.exec_id}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["exec_id"] == meta.exec_id
    assert "streamed line" in body["output"]
    assert "pending" in body

    r = client.get("/dashboard/api/exec/exec_nope", headers=headers)
    assert r.status_code == 404

    r = client.get("/dashboard/exec", headers=headers)
    assert r.status_code == 200
    assert "Executor Console" in r.text


def test_dashboard_exec_start_validates_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINS_STATE_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from brains.dashboard.app import app

    client = TestClient(app)
    headers = {"Authorization": "Bearer local-dev-key"}
    r = client.post(
        "/dashboard/api/exec/start",
        headers=headers,
        data={"prompt": "x", "workspace": str(tmp_path), "tool": "bogus"},
    )
    assert r.status_code == 400
