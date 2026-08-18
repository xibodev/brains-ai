"""Tests for the native-battalion WS2 layer.

Covers the additive Org / Persona / Runtime / Project / Issue schema:

* every new table provisions through the migration contract (frozen baseline
  DDL plus the numbered deltas), not from the installed models;
* the 120 / 121 disk migrations are idempotent (safe to run twice) and the
  120 migration seeds a default org + backfills ``workspaces.org_id``;
* each control module's happy path and key invariants — unique constraints,
  issue status transitions (``closed_at`` stamping), tri-modal assignment,
  runtime upsert idempotency, and ``sweep_stale`` flipping online → offline.

Follows ``tests/test_squads.py`` style: plain pytest functions + fixtures, on
the conftest-isolated tmp DB.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sqlite3

import pytest
from sqlalchemy import inspect

from brains.control import issues, orgs, personas, projects, runtimes, squads
from brains.control.operators import add_operator, ensure_admin_operator
from brains.storage.db import engine
from brains.storage.migrations import SQL_MIGRATIONS_DIR, init_db

NEW_TABLES = ("orgs", "org_members", "runtimes", "personas", "projects", "issues")


def _ensure_operator(slug: str) -> None:
    with contextlib.suppress(Exception):
        add_operator(slug)


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    ensure_admin_operator()
    for slug in ("alice", "bob", "carol"):
        _ensure_operator(slug)
    yield


@pytest.fixture
def workspace(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


@pytest.fixture
def org():
    # Unique slug per test to dodge the cross-test shared-DB unique constraint.
    import uuid

    slug = f"org-{uuid.uuid4().hex[:8]}"
    return orgs.create_org(slug, "Acme")


# --------------------------------------------------------------------------- #
# Schema provisioning
# --------------------------------------------------------------------------- #


def test_new_tables_provisioned_by_the_migration_contract():
    init_db()
    existing = set(inspect(engine).get_table_names())
    for table in NEW_TABLES:
        assert table in existing, f"{table} not provisioned by the migration contract"


def test_existing_tables_gained_link_columns():
    init_db()
    insp = inspect(engine)
    ws_cols = {c["name"] for c in insp.get_columns("workspaces")}
    assert "org_id" in ws_cols
    session_cols = {c["name"] for c in insp.get_columns("agent_sessions")}
    assert {"issue_id", "persona_id", "runtime_id"} <= session_cols


# --------------------------------------------------------------------------- #
# Disk migrations — idempotency + seed/backfill
# --------------------------------------------------------------------------- #


def _load_migration(filename: str):
    path = SQL_MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raw_sqlite_conn() -> sqlite3.Connection:
    raw = engine.raw_connection()
    conn = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
    assert isinstance(conn, sqlite3.Connection)
    return conn


def test_migration_120_idempotent_and_seeds_default_org():
    init_db()
    mig = _load_migration("120_org_workspace.py")
    conn = _raw_sqlite_conn()
    # Run twice — must not raise and must not duplicate the default org.
    mig.upgrade(conn)
    conn.commit()
    mig.upgrade(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM orgs WHERE slug='default'").fetchone()[0]
    assert count == 1


def test_migration_120_backfills_null_workspace_org(workspace):
    from brains.control.sessions import register_workspace

    init_db()
    ws = register_workspace(workspace)
    ws_id = ws.id
    conn = _raw_sqlite_conn()
    # Force the column NULL to simulate a pre-pivot row, then backfill.
    conn.execute("UPDATE workspaces SET org_id = NULL WHERE id = ?", (ws_id,))
    conn.commit()
    mig = _load_migration("120_org_workspace.py")
    mig.upgrade(conn)
    conn.commit()
    row = conn.execute("SELECT org_id FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
    default_id = conn.execute("SELECT id FROM orgs WHERE slug='default'").fetchone()[0]
    assert row[0] == default_id


def test_migration_121_idempotent():
    init_db()
    mig = _load_migration("121_session_links.py")
    conn = _raw_sqlite_conn()
    mig.upgrade(conn)
    conn.commit()
    mig.upgrade(conn)
    conn.commit()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")}
    assert {"issue_id", "persona_id", "runtime_id"} <= cols


# --------------------------------------------------------------------------- #
# orgs
# --------------------------------------------------------------------------- #


def test_create_and_get_org(org):
    fetched = orgs.get_org(org["slug"])
    assert fetched["id"] == org["id"]
    by_id = orgs.get_org(org["id"])
    assert by_id["slug"] == org["slug"]


def test_create_org_rejects_duplicate_slug(org):
    with pytest.raises(ValueError, match="already exists"):
        orgs.create_org(org["slug"], "Dup")


def test_create_org_rejects_bad_slug():
    with pytest.raises(ValueError, match="slug"):
        orgs.create_org("Not A Slug", "X")


def test_ensure_default_org_idempotent():
    a = orgs.ensure_default_org()
    b = orgs.ensure_default_org()
    assert a["id"] == b["id"]
    assert a["slug"] == "default"


def test_add_member_idempotent_updates_role(org):
    orgs.add_member(org["id"], "alice", role="member")
    orgs.add_member(org["id"], "alice", role="admin")
    members = orgs.list_members(org["id"])
    alice = [m for m in members if m["operator"] == "alice"]
    assert len(alice) == 1
    assert alice[0]["role"] == "admin"


def test_add_member_unknown_operator(org):
    with pytest.raises(ValueError, match="unknown operator"):
        orgs.add_member(org["id"], "nobody")


# --------------------------------------------------------------------------- #
# runtimes
# --------------------------------------------------------------------------- #


def test_register_runtime_upsert_idempotent():
    a = runtimes.register_runtime("machine-1", "copilot")
    b = runtimes.register_runtime("machine-1", "copilot", health="healthy")
    assert a["id"] == b["id"]
    listed = runtimes.list_runtimes(machine_id="machine-1", tool="copilot")
    assert len(listed) == 1
    assert listed[0]["health"] == "healthy"


def test_register_runtime_autoregisters_tool():
    from brains.control.tool_registry import list_registered_tools

    runtimes.register_runtime("machine-2", "claude-code")
    names = {t["name"] for t in list_registered_tools()}
    assert "claude-code" in names


def test_heartbeat_brings_offline_runtime_online():
    rt = runtimes.register_runtime("machine-3", "copilot")
    runtimes.mark_offline(rt["id"])
    assert runtimes.get_runtime(rt["id"])["status"] == "offline"
    after = runtimes.heartbeat(rt["id"])
    assert after["status"] == "online"


def test_sweep_stale_flips_online_to_offline():
    rt = runtimes.register_runtime("machine-4", "copilot")
    assert rt["status"] == "online"
    flipped = runtimes.sweep_stale(ttl_seconds=-1)
    flipped_slugs = {f["slug"] for f in flipped}
    assert rt["slug"] in flipped_slugs
    assert runtimes.get_runtime(rt["id"])["status"] == "offline"
    # An already-offline runtime is not a sweep candidate.
    again = runtimes.sweep_stale(ttl_seconds=-1)
    assert rt["slug"] not in {f["slug"] for f in again}


def test_count_stale_is_read_only_and_matches_sweep_candidates():
    rt = runtimes.register_runtime("machine-count-stale", "copilot")
    assert rt["status"] == "online"
    # A generous TTL: this fresh runtime is not a candidate yet.
    assert runtimes.count_stale(ttl_seconds=3600) == 0
    # ttl=-1 always counts every online runtime as stale, exactly like
    # sweep_stale's own candidate selection - but count_stale must not flip it.
    assert runtimes.count_stale(ttl_seconds=-1) >= 1
    assert runtimes.get_runtime(rt["id"])["status"] == "online"


def test_deregister_removes_runtime():
    rt = runtimes.register_runtime("machine-5", "copilot")
    runtimes.deregister(rt["id"])
    assert runtimes.get_runtime(rt["id"]) is None


# --------------------------------------------------------------------------- #
# personas
# --------------------------------------------------------------------------- #


def test_create_persona_and_unique_org_slug(org):
    p = personas.create_persona(org["id"], "mason", "Mason the Builder")
    assert p["slug"] == "mason"
    assert p["operator_id"] is None
    with pytest.raises(ValueError, match="already exists"):
        personas.create_persona(org["id"], "mason", "Dup")


def test_create_persona_unknown_org():
    with pytest.raises(ValueError, match="unknown org"):
        personas.create_persona(999999, "x", "X")


def test_bind_operator_one_to_one(org):
    p = personas.create_persona(org["id"], "scout", "Scout")
    bound = personas.bind_operator(p["id"], "alice")
    assert bound["operator_id"] is not None
    # Re-binding to the same operator is idempotent.
    again = personas.bind_operator(p["id"], "alice")
    assert again["operator_id"] == bound["operator_id"]
    # Re-binding to a different operator is rejected.
    with pytest.raises(ValueError, match="already bound"):
        personas.bind_operator(p["id"], "bob")


def test_update_and_archive_persona(org):
    p = personas.create_persona(org["id"], "ranger", "Ranger")
    personas.update(p["id"], model="gpt-5", color="#ff0000")
    updated = personas.get_persona(p["id"])
    assert updated["model"] == "gpt-5"
    assert updated["color"] == "#ff0000"
    personas.archive(p["id"])
    assert personas.get_persona(p["id"])["status"] == "archived"
    active = personas.list_personas(org_id=org["id"])
    assert all(x["slug"] != "ranger" for x in active)


def test_update_persona_rejects_unknown_field(org):
    p = personas.create_persona(org["id"], "smith", "Smith")
    with pytest.raises(ValueError, match="cannot update"):
        personas.update(p["id"], bogus="x")


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #


def test_create_project_mints_code(org):
    p = projects.create_project(org["id"], "apollo", "Apollo")
    assert p["code"].startswith("PRJ-")
    fetched = projects.get_project(p["code"])
    assert fetched["id"] == p["id"]


def test_create_project_unique_org_slug(org):
    projects.create_project(org["id"], "gemini", "Gemini")
    with pytest.raises(ValueError, match="already exists"):
        projects.create_project(org["id"], "gemini", "Dup")


def test_project_update_and_archive(org):
    p = projects.create_project(org["id"], "mercury", "Mercury")
    projects.update(p["id"], status="paused", name="Mercury II")
    assert projects.get_project(p["id"])["status"] == "paused"
    projects.archive(p["id"])
    assert projects.get_project(p["id"])["status"] == "archived"
    listed = projects.list_projects(org_id=org["id"])
    assert all(x["slug"] != "mercury" for x in listed)


def test_project_update_rejects_bad_status(org):
    p = projects.create_project(org["id"], "saturn", "Saturn")
    with pytest.raises(ValueError, match="status"):
        projects.update(p["id"], status="nonsense")


# --------------------------------------------------------------------------- #
# issues
# --------------------------------------------------------------------------- #


def test_create_issue_mints_code(org):
    proj = projects.create_project(org["id"], "voyager", "Voyager")
    iss = issues.create_issue(proj["id"], "Fix the thruster")
    assert iss["code"].startswith("ISS-")
    assert iss["status"] == "open"
    assert iss["closed_at"] is None


def test_create_issue_unknown_project():
    with pytest.raises(ValueError, match="unknown project"):
        issues.create_issue(999999, "x")


def test_issue_transition_stamps_closed_at(org):
    proj = projects.create_project(org["id"], "pioneer", "Pioneer")
    iss = issues.create_issue(proj["id"], "Land it")
    moved = issues.transition(iss["code"], "in_progress")
    assert moved["closed_at"] is None
    done = issues.transition(iss["code"], "done")
    assert done["status"] == "done"
    assert done["closed_at"] is not None
    # Re-opening clears closed_at.
    reopened = issues.transition(iss["code"], "open")
    assert reopened["closed_at"] is None


def test_issue_transition_rejects_bad_status(org):
    proj = projects.create_project(org["id"], "horizon", "Horizon")
    iss = issues.create_issue(proj["id"], "Scan")
    with pytest.raises(ValueError, match="status"):
        issues.transition(iss["code"], "frozen")


def test_issue_assign_tri_modal(org, workspace):
    proj = projects.create_project(org["id"], "atlas", "Atlas")
    iss = issues.create_issue(proj["id"], "Carry the sky")
    persona = personas.create_persona(org["id"], "titan", "Titan")
    squads.create_squad(workspace, "ops", "Ops Pod", leader="alice")
    # Resolve the squad row id for assignment.
    from brains.storage.db import SessionLocal
    from brains.storage.models import Squad

    with SessionLocal() as s:
        pod_id = s.query(Squad).filter(Squad.slug == "ops").one().id

    # persona
    a = issues.assign(iss["code"], persona_id=persona["id"])
    assert a["assignee_persona_id"] == persona["id"]
    assert a["assignee_pod_id"] is None
    # pod (clears persona)
    b = issues.assign(iss["code"], pod_id=pod_id)
    assert b["assignee_pod_id"] == pod_id
    assert b["assignee_persona_id"] is None
    # operator (clears pod)
    c = issues.assign(iss["code"], operator="bob")
    assert c["assignee_operator_id"] is not None
    assert c["assignee_pod_id"] is None


def test_issue_assign_requires_exactly_one_target(org):
    proj = projects.create_project(org["id"], "odyssey", "Odyssey")
    iss = issues.create_issue(proj["id"], "Sail")
    with pytest.raises(ValueError, match="exactly one"):
        issues.assign(iss["code"])
    persona = personas.create_persona(org["id"], "helm", "Helm")
    with pytest.raises(ValueError, match="exactly one"):
        issues.assign(iss["code"], persona_id=persona["id"], operator="alice")


def test_issue_assign_unknown_persona(org):
    proj = projects.create_project(org["id"], "nova", "Nova")
    iss = issues.create_issue(proj["id"], "Ignite")
    with pytest.raises(ValueError, match="unknown persona"):
        issues.assign(iss["code"], persona_id=999999)


def test_list_issues_filters_by_project_and_status(org):
    proj = projects.create_project(org["id"], "lyra", "Lyra")
    i1 = issues.create_issue(proj["id"], "One")
    issues.create_issue(proj["id"], "Two")
    issues.transition(i1["code"], "in_progress")
    open_issues = issues.list_issues(project_id=proj["id"], status="open")
    assert len(open_issues) == 1
    assert open_issues[0]["title"] == "Two"
