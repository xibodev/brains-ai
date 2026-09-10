"""Tests for the Phase 2 cross-operator knowledge ledger."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.storage.migrations import init_db


@pytest.fixture
def isolated_brains(tmp_path, monkeypatch):
    """Per-test DB + audit/state isolation (mirrors tests/test_memberships.py)."""
    db_path = tmp_path / "isolated.sqlite"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("BRAINS_STATE_DIR", str(state))
    monkeypatch.setenv("BRAINS_AUDIT_KEY_FILE", str(tmp_path / "audit-key"))
    monkeypatch.delenv("BRAINS_AUDIT_KEY", raising=False)
    monkeypatch.delenv("BRAINS_OPERATOR", raising=False)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = db_module.sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(migrations_module, "engine", engine)
    monkeypatch.setattr(migrations_module, "SessionLocal", SessionLocal)

    import brains.audit as audit_module
    import brains.control.events as events_module
    import brains.control.sessions as sessions_module

    for mod in (audit_module, events_module, sessions_module):
        monkeypatch.setattr(mod, "SessionLocal", SessionLocal, raising=False)

    from brains.audit import _reset_key_cache

    _reset_key_cache()
    init_db()
    yield tmp_path
    _reset_key_cache()


def _make_workspace(path, slug: str, visibility: str = "shared") -> int:
    from brains.control.memberships import set_workspace_visibility
    from brains.control.sessions import register_workspace

    path.mkdir(parents=True, exist_ok=True)
    ws = register_workspace(str(path), slug=slug)
    if visibility != "shared":
        set_workspace_visibility(slug, visibility)
    return ws.id


def _set_current_operator(monkeypatch, slug: str) -> None:
    monkeypatch.setenv("BRAINS_OPERATOR", slug)


def test_init_db_creates_knowledge_entries_table(isolated_brains):
    conn = sqlite3.connect(str(isolated_brains / "isolated.sqlite"))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "knowledge_entries" in tables
    finally:
        conn.close()


def test_init_db_creates_knowledge_v2_columns(isolated_brains):
    conn = sqlite3.connect(str(isolated_brains / "isolated.sqlite"))
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_entries)")}
        assert {"provenance", "importance", "valid_until", "promoted_from"} <= columns
    finally:
        conn.close()


def test_add_and_search_round_trip(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry, search_knowledge

    ws = tmp_path / "payments-api"
    _make_workspace(ws, "payments-api")
    entry = add_knowledge_entry(
        str(ws),
        "blocker",
        "Terraform AWS provider mismatch",
        body="pin provider version X",
        tags="terraform,aws",
    )
    assert entry["code"].startswith("KNOW-")
    assert entry["type"] == "blocker"
    assert entry["status"] == "active"

    results = search_knowledge(query="terraform")
    assert any(r["code"] == entry["code"] for r in results)
    typed = search_knowledge(type="blocker")
    assert any(r["code"] == entry["code"] for r in typed)


def test_context_compression_disabled_preserves_search_body(isolated_brains, tmp_path, monkeypatch):
    from brains.config import settings
    from brains.control.knowledge import add_knowledge_entry, search_knowledge

    monkeypatch.setattr(settings, "context_compression_enabled", False)
    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    body = "full body " * 40
    entry = add_knowledge_entry(str(ws), "blocker", "Uncompressed entry", body=body)

    row = next(r for r in search_knowledge(query="Uncompressed") if r["code"] == entry["code"])
    assert row["body"] == body
    assert "ref" not in row


def test_context_compression_round_trips_via_retrieve_original(
    isolated_brains, tmp_path, monkeypatch
):
    from brains.config import settings
    from brains.control.knowledge import add_knowledge_entry, search_knowledge
    from brains.control.retrieve import retrieve_original

    monkeypatch.setattr(settings, "context_compression_enabled", True)
    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    body = "roundtrip body " * 30
    entry = add_knowledge_entry(str(ws), "blocker", "Compressed entry", body=body)

    row = next(r for r in search_knowledge(query="Compressed") if r["code"] == entry["code"])
    assert row["ref"] == f"knowledge:{entry['code']}"
    assert row["body"] == body[:200]
    assert row["body"] != body
    original = retrieve_original(row["ref"])
    assert original["content"] == body
    assert original["body"] == body


def test_search_ranks_by_importance(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry, search_knowledge

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    low = add_knowledge_entry(str(ws), "blocker", "low importance", importance=0.1)
    high = add_knowledge_entry(str(ws), "blocker", "high importance", importance=0.9)

    results = search_knowledge(type="blocker")
    codes = [row["code"] for row in results]
    assert codes.index(high["code"]) < codes.index(low["code"])


def test_expire_stale_knowledge_marks_due_entries(isolated_brains, tmp_path):
    from brains.control.knowledge import (
        add_knowledge_entry,
        expire_stale_knowledge,
        search_knowledge,
    )

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    past = datetime.now(UTC) - timedelta(minutes=5)
    future = datetime.now(UTC) + timedelta(minutes=5)
    stale = add_knowledge_entry(
        str(ws),
        "blocker",
        "expired blocker",
        valid_until=past.isoformat(),
    )
    fresh = add_knowledge_entry(
        str(ws),
        "blocker",
        "fresh blocker",
        valid_until=future,
    )

    assert expire_stale_knowledge() == 1
    stale_codes = {row["code"] for row in search_knowledge(status="stale")}
    active_codes = {row["code"] for row in search_knowledge(status="active")}
    assert stale["code"] in stale_codes
    assert fresh["code"] in active_codes


def test_supersede_marks_old_entry(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry, search_knowledge

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    old = add_knowledge_entry(str(ws), "workaround", "pin provider 4.0")
    new = add_knowledge_entry(str(ws), "resolution", "upgrade module", supersedes_code=old["code"])
    assert new["supersedes"] == old["code"]
    superseded = [r for r in search_knowledge(status="superseded") if r["code"] == old["code"]]
    assert len(superseded) == 1
    assert superseded[0]["superseded_by_id"] is not None


def test_supersede_next_code_does_not_create_self_reference(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry
    from brains.storage.models import KnowledgeEntry

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    with pytest.raises(ValueError, match="unknown or inaccessible knowledge entry"):
        add_knowledge_entry(
            str(ws), "resolution", "Missing predecessor", supersedes_code="KNOW-0001"
        )
    with db_module.SessionLocal() as session:
        assert session.query(KnowledgeEntry).count() == 0


def test_resolve_sets_status_and_timestamp(isolated_brains, tmp_path):
    from brains.control.knowledge import (
        add_knowledge_entry,
        resolve_knowledge_entry,
        search_knowledge,
    )

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    e = add_knowledge_entry(str(ws), "blocker", "CI failing after dep update")
    resolve_knowledge_entry(e["code"], status="resolved")
    got = [r for r in search_knowledge(status="resolved") if r["code"] == e["code"]]
    assert len(got) == 1
    assert got[0]["resolved_at"] is not None


def test_invalid_type_and_scope_raise(isolated_brains, tmp_path):
    from brains.control.knowledge import add_knowledge_entry

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    with pytest.raises(ValueError):
        add_knowledge_entry(str(ws), "not-a-type", "x")
    with pytest.raises(ValueError):
        add_knowledge_entry(str(ws), "blocker", "x", scope="universe")
    with pytest.raises(ValueError):
        add_knowledge_entry(str(ws), "blocker", "x", provenance="guessed")


def test_search_respects_visibility(isolated_brains, tmp_path, monkeypatch):
    from brains.control.knowledge import add_knowledge_entry, search_knowledge
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")

    # A workspace-scoped entry in a private workspace, and a brain-wide shared one.
    priv = add_knowledge_entry(str(ws_private), "blocker", "secret blocker", scope="workspace")
    shared = add_knowledge_entry(str(ws_private), "caveat", "general caveat", scope="shared")

    admin_codes = {r["code"] for r in search_knowledge()}
    assert priv["code"] in admin_codes
    assert shared["code"] in admin_codes

    # Alice is not a member of the private workspace: she sees only the shared entry.
    _set_current_operator(monkeypatch, "alice")
    alice_codes = {r["code"] for r in search_knowledge()}
    assert shared["code"] in alice_codes
    assert priv["code"] not in alice_codes


def test_retrieve_original_respects_knowledge_visibility(isolated_brains, tmp_path, monkeypatch):
    from brains.control.knowledge import add_knowledge_entry
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.control.retrieve import retrieve_original

    ensure_admin_operator()
    add_operator("alice")
    ws_private = tmp_path / "ws-private"
    _make_workspace(ws_private, "ws-private", visibility="private")
    priv = add_knowledge_entry(
        str(ws_private),
        "blocker",
        "private blocker",
        body="private body",
        scope="workspace",
    )
    shared = add_knowledge_entry(
        str(ws_private),
        "caveat",
        "shared caveat",
        body="shared body",
        scope="shared",
    )

    assert retrieve_original(f"knowledge:{priv['code']}")["content"] == "private body"
    _set_current_operator(monkeypatch, "alice")
    with pytest.raises(ValueError, match="inaccessible"):
        retrieve_original(f"knowledge:{priv['code']}")
    assert retrieve_original(f"knowledge:{shared['code']}")["content"] == "shared body"


@pytest.mark.parametrize(
    ("stored", "deadline", "linked", "status", "freshness", "expired", "superseded"),
    [
        ("active", None, False, "active", "current", False, False),
        ("confirmed", -1, False, "stale", "expired", True, False),
        ("active", -1, False, "stale", "expired", True, False),
        ("confirmed", 0, False, "confirmed", "current", False, False),
        ("stale", -1, False, "stale", "stale", True, False),
        ("resolved", -1, False, "resolved", "historical", True, False),
        ("rejected", None, False, "rejected", "historical", False, False),
        ("superseded", None, False, "superseded", "superseded", False, True),
        ("active", None, True, "superseded", "superseded", False, True),
        ("active", -1, True, "superseded", "expired", True, True),
    ],
)
def test_lifecycle_contract(stored, deadline, linked, status, freshness, expired, superseded):
    from brains.control.knowledge import knowledge_lifecycle
    from brains.storage.models import KnowledgeEntry

    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = KnowledgeEntry(
        status=stored,
        valid_until=(now + timedelta(seconds=deadline)).replace(tzinfo=None)
        if deadline is not None
        else None,
        superseded_by_id=1 if linked else None,
    )
    expected = {
        "status": status,
        "stored_status": stored,
        "freshness": freshness,
        "expired": expired,
        "superseded": superseded,
    }
    assert knowledge_lifecycle(row, now) == expected
    assert knowledge_lifecycle(row, now.replace(tzinfo=None)) == expected
    assert knowledge_lifecycle(row, now.astimezone(timezone(timedelta(hours=5)))) == expected
    assert row.status == stored


@pytest.mark.parametrize("stored", ["active", "confirmed"])
def test_search_effective_status_before_limit_without_writes(
    isolated_brains, tmp_path, monkeypatch, stored
):
    import brains.control.knowledge as knowledge
    from brains.storage.models import KnowledgeEntry

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(knowledge, "utc_now", lambda: now)
    due = knowledge.add_knowledge_entry(
        str(ws),
        "caveat",
        "due",
        importance=1,
        valid_until=(now - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=5))),
    )
    fresh = knowledge.add_knowledge_entry(
        str(ws),
        "caveat",
        "fresh",
        importance=0,
        valid_until=now,
    )
    for entry in (due, fresh):
        knowledge.resolve_knowledge_entry(entry["code"], stored)
    with db_module.SessionLocal() as session:
        before = [
            (r.code, r.status, r.updated_at, r.resolved_at, r.valid_until)
            for r in session.query(KnowledgeEntry).order_by(KnowledgeEntry.id)
        ]
    results = knowledge.search_knowledge()
    assert [r["code"] for r in results] == [due["code"], fresh["code"]]
    assert results[0]["stored_status"] == stored
    assert results[0]["status"] == "stale"
    assert results[0]["freshness"] == "expired"
    assert [r["code"] for r in knowledge.search_knowledge(status=stored, limit=1)] == [
        fresh["code"]
    ]
    assert [r["code"] for r in knowledge.search_knowledge(status="stale")] == [due["code"]]
    with db_module.SessionLocal() as session:
        after = [
            (r.code, r.status, r.updated_at, r.resolved_at, r.valid_until)
            for r in session.query(KnowledgeEntry).order_by(KnowledgeEntry.id)
        ]
    assert before == after


def test_superseded_history_cannot_be_reactivated(isolated_brains, tmp_path):
    from brains.control.knowledge import (
        add_knowledge_entry,
        expire_stale_knowledge,
        resolve_knowledge_entry,
        search_knowledge,
    )
    from brains.storage.models import KnowledgeEntry

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    old = add_knowledge_entry(
        str(ws),
        "caveat",
        "old",
        body="historical evidence",
        evidence="fixture:source",
        provenance="extracted",
        importance=1,
        valid_until=datetime.now(UTC) - timedelta(days=1),
    )
    new = add_knowledge_entry(str(ws), "resolution", "new", supersedes_code=old["code"])
    for status in ("active", "confirmed"):
        with pytest.raises(ValueError, match="superseded"):
            resolve_knowledge_entry(old["code"], status)
    # Retained or externally-written rows cannot masquerade as current either.
    with db_module.SessionLocal() as session:
        session.query(KnowledgeEntry).filter_by(code=old["code"]).update({"status": "active"})
        session.commit()
    assert [r["code"] for r in search_knowledge(status="active", limit=1)] == [new["code"]]
    assert search_knowledge(status="stale") == []
    historical = search_knowledge(status="superseded")[0]
    assert historical["code"] == old["code"]
    assert historical["body"] == "historical evidence"
    assert historical["evidence"] == "fixture:source"
    assert historical["provenance"] == "extracted"
    assert historical["superseded"] is True
    assert expire_stale_knowledge() == 0


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("body", ["small body", "é" * 400, "界" * 22000])
def test_search_utf8_bounds_keep_stored_history(
    isolated_brains, tmp_path, monkeypatch, compressed, body
):
    from brains.config import settings
    from brains.control.knowledge import CONTENT_LIMIT_BYTES, add_knowledge_entry, search_knowledge
    from brains.storage.models import KnowledgeEntry

    monkeypatch.setattr(settings, "context_compression_enabled", compressed)
    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    added = add_knowledge_entry(str(ws), "caveat", "bounded", body=body, evidence="fixture:proof")
    row = search_knowledge()[0]
    expected = body.encode("utf-8")[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="ignore")
    if compressed:
        expected = expected[:200]
    assert row["body"] == expected
    assert len(row["body"].encode("utf-8")) <= CONTENT_LIMIT_BYTES
    assert row["content_bytes"] == len(body.encode("utf-8"))
    assert row["content_limit_bytes"] == CONTENT_LIMIT_BYTES
    assert row["truncated"] == (body != expected)
    assert row["evidence"] == "fixture:proof"
    if compressed or row["truncated"]:
        assert row["ref"] == f"knowledge:{added['code']}"
    else:
        assert "ref" not in row
    with db_module.SessionLocal() as session:
        assert session.query(KnowledgeEntry).filter_by(code=added["code"]).one().body == body


@pytest.mark.parametrize(("limit", "expected"), [(-1, 1), (0, 1), (1, 1), (101, 100)])
def test_search_limits_are_bounded(isolated_brains, limit, expected):
    from brains.control.knowledge import search_knowledge
    from brains.storage.models import KnowledgeEntry

    with db_module.SessionLocal() as session:
        session.add_all(
            [
                KnowledgeEntry(code=f"KNOW-{i:04d}", type="caveat", title="fixture", scope="global")
                for i in range(101)
            ]
        )
        session.commit()
    assert len(search_knowledge(limit=limit)) == expected


@pytest.mark.parametrize("operation", ["supersede", "promote", "resolve"])
def test_hidden_write_references_match_missing_and_leave_history_intact(
    isolated_brains, tmp_path, monkeypatch, operation
):
    from brains.control.knowledge import add_knowledge_entry, resolve_knowledge_entry
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.storage.models import KnowledgeEntry

    admin = ensure_admin_operator()
    add_operator("alice")
    private = tmp_path / "private"
    public = tmp_path / "public"
    _make_workspace(private, "private", visibility="private")
    _make_workspace(public, "public")
    old = add_knowledge_entry(str(private), "caveat", "hidden", evidence="fixture:private")
    _set_current_operator(monkeypatch, "alice")
    for code in (old["code"], "KNOW-9999"):
        with pytest.raises(ValueError) as exc:
            if operation == "resolve":
                resolve_knowledge_entry(code)
            else:
                kwargs = {"supersedes_code" if operation == "supersede" else "promoted_from": code}
                # Attribution must not grant the caller admin visibility.
                add_knowledge_entry(
                    str(public), "resolution", "replacement", operator_id=admin["id"], **kwargs
                )
        assert str(exc.value) == f"unknown or inaccessible knowledge entry: {code}"
    with db_module.SessionLocal() as session:
        assert session.query(KnowledgeEntry).count() == 1
        row = session.query(KnowledgeEntry).one()
        assert row.status == "active"
        assert row.superseded_by_id is None
        assert row.evidence == "fixture:private"


@pytest.mark.parametrize("scope", ["shared", "global"])
def test_visible_historical_entries_retain_evidence(isolated_brains, tmp_path, monkeypatch, scope):
    from brains.control.knowledge import (
        add_knowledge_entry,
        resolve_knowledge_entry,
        search_knowledge,
    )
    from brains.control.operators import add_operator, ensure_admin_operator

    ensure_admin_operator()
    add_operator("alice")
    ws = tmp_path / "private"
    _make_workspace(ws, "private", visibility="private")
    old = add_knowledge_entry(
        str(ws),
        "workaround",
        "old",
        scope=scope,
        evidence="fixture:original",
        provenance="extracted",
    )
    _set_current_operator(monkeypatch, "alice")
    resolve_knowledge_entry(old["code"])
    new = add_knowledge_entry(
        str(ws),
        "resolution",
        "new",
        scope=scope,
        supersedes_code=old["code"],
        promoted_from=old["code"],
    )
    rows = {r["code"]: r for r in search_knowledge()}
    assert set(rows) == {old["code"], new["code"]}
    assert rows[old["code"]]["freshness"] == "superseded"
    assert rows[old["code"]]["evidence"] == "fixture:original"
    assert rows[old["code"]]["provenance"] == "extracted"
    assert rows[new["code"]]["promoted_from"] is not None


@pytest.mark.parametrize("compressed", [False, True])
def test_search_hides_inaccessible_successor_id(isolated_brains, tmp_path, monkeypatch, compressed):
    from brains.config import settings
    from brains.control.knowledge import add_knowledge_entry, search_knowledge
    from brains.control.operators import add_operator, ensure_admin_operator

    monkeypatch.setattr(settings, "context_compression_enabled", compressed)
    ensure_admin_operator()
    add_operator("alice")
    ws = tmp_path / "private"
    _make_workspace(ws, "private", visibility="private")
    old = add_knowledge_entry(str(ws), "caveat", "shared predecessor", scope="shared")
    new = add_knowledge_entry(
        str(ws), "resolution", "hidden successor", supersedes_code=old["code"]
    )
    admin_result = search_knowledge(status="superseded", limit=1)[0]
    assert admin_result["superseded_by_id"] is not None
    _set_current_operator(monkeypatch, "alice")
    for kwargs in ({}, {"status": "superseded", "limit": 1}):
        rows = search_knowledge(**kwargs)
        assert [row["code"] for row in rows] == [old["code"]]
        assert rows[0]["superseded_by_id"] is None
        assert rows[0]["superseded"] is True
        assert rows[0]["status"] == "superseded"
        assert rows[0]["freshness"] == "superseded"
        assert new["code"] not in str(rows)


@pytest.mark.parametrize("compressed", [False, True])
def test_evidence_utf8_bound_and_aggregate_truncation(
    isolated_brains, tmp_path, monkeypatch, compressed
):
    from brains.config import settings
    from brains.control.knowledge import CONTENT_LIMIT_BYTES, add_knowledge_entry, search_knowledge
    from brains.storage.models import KnowledgeEntry

    monkeypatch.setattr(settings, "context_compression_enabled", compressed)
    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    evidence = "界" * 22000
    added = add_knowledge_entry(
        str(ws), "caveat", "large evidence", body="small", evidence=evidence
    )
    result = search_knowledge()[0]
    expected = evidence.encode("utf-8")[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="ignore")
    for row in (added, result):
        assert row["evidence"] == expected
        assert len(row["evidence"].encode("utf-8")) <= CONTENT_LIMIT_BYTES
        assert "\ufffd" not in row["evidence"]
        assert row["evidence_bytes"] == len(evidence.encode("utf-8"))
        assert row["evidence_truncated"] is True
        assert row["body_truncated"] is False
        assert row["truncated"] is True
        assert row["body"] == "small"
        assert row["content_bytes"] == 5
        assert row["content_limit_bytes"] == CONTENT_LIMIT_BYTES
        assert row["ref"] == f"knowledge:{added['code']}"
    with db_module.SessionLocal() as session:
        assert (
            session.query(KnowledgeEntry).filter_by(code=added["code"]).one().evidence == evidence
        )


@pytest.mark.parametrize("stored", ["superseded", "active"])
def test_repeated_supersede_preserves_chain_and_rolls_back_new_entry(
    isolated_brains, tmp_path, monkeypatch, stored
):
    import brains.control.knowledge as knowledge
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.storage.models import KnowledgeEntry

    ensure_admin_operator()
    add_operator("alice")
    ws = tmp_path / "private"
    _make_workspace(ws, "private", visibility="private")
    old = knowledge.add_knowledge_entry(str(ws), "caveat", "old", scope="shared")
    first = knowledge.add_knowledge_entry(
        str(ws), "resolution", "first", supersedes_code=old["code"]
    )
    with db_module.SessionLocal() as session:
        session.query(KnowledgeEntry).filter_by(code=old["code"]).update({"status": stored})
        session.commit()
        before = session.query(KnowledgeEntry).filter_by(code=old["code"]).one()
        original = (before.status, before.superseded_by_id, before.resolved_at, before.updated_at)
    _set_current_operator(monkeypatch, "alice")
    events = []
    monkeypatch.setattr(knowledge, "append_event", lambda *args, **kwargs: events.append(args))
    with pytest.raises(ValueError) as exc:
        knowledge.add_knowledge_entry(str(ws), "resolution", "second", supersedes_code=old["code"])
    assert str(exc.value) == f"knowledge entry changed or already superseded: {old['code']}"
    assert first["code"] not in str(exc.value)
    assert events == []
    with db_module.SessionLocal() as session:
        assert session.query(KnowledgeEntry).count() == 2
        after = session.query(KnowledgeEntry).filter_by(code=old["code"]).one()
        assert (
            after.status,
            after.superseded_by_id,
            after.resolved_at,
            after.updated_at,
        ) == original


@pytest.mark.parametrize("status", ["active", "confirmed", "resolved", "rejected", "stale"])
@pytest.mark.parametrize("competing_change", ["status", "link"])
def test_resolve_compare_and_swap_rejects_interleaved_committed_change(
    isolated_brains, tmp_path, monkeypatch, status, competing_change
):
    from sqlalchemy.orm import Query

    import brains.control.knowledge as knowledge
    from brains.storage.models import KnowledgeEntry

    ws = tmp_path / "ws"
    _make_workspace(ws, "ws")
    old = knowledge.add_knowledge_entry(str(ws), "caveat", "old")
    successor = knowledge.add_knowledge_entry(str(ws), "resolution", "successor")
    with db_module.SessionLocal() as session:
        successor_id = session.query(KnowledgeEntry).filter_by(code=successor["code"]).one().id
    original_update = Query.update
    armed = True

    def interleave(query, *args, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            # The resolver has read its snapshot and built its UPDATE. Commit a
            # separate writer before that UPDATE executes, without sleeps/locks.
            with db_module.SessionLocal() as other:
                values = (
                    {"status": "rejected"}
                    if competing_change == "status"
                    else {"superseded_by_id": successor_id}
                )
                original_update(other.query(KnowledgeEntry).filter_by(code=old["code"]), values)
                other.commit()
        return original_update(query, *args, **kwargs)

    monkeypatch.setattr(Query, "update", interleave)
    events = []
    monkeypatch.setattr(knowledge, "append_event", lambda *args, **kwargs: events.append(args))
    with pytest.raises(ValueError, match="knowledge entry changed"):
        knowledge.resolve_knowledge_entry(old["code"], status)
    assert not armed
    assert events == []
    with db_module.SessionLocal() as session:
        row = session.query(KnowledgeEntry).filter_by(code=old["code"]).one()
        assert row.status == ("rejected" if competing_change == "status" else "active")
        assert row.superseded_by_id == (successor_id if competing_change == "link" else None)
        assert row.resolved_at is None
