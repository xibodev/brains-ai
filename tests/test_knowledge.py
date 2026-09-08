"""Tests for the Phase 2 cross-operator knowledge ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

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


@pytest.mark.parametrize("scope", ["shared", "global"])
def test_retrieve_original_respects_knowledge_visibility(
    isolated_brains, tmp_path, monkeypatch, scope
):
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
        scope=scope,
    )

    assert retrieve_original(f"knowledge:{priv['code']}")["content"] == "private body"
    _set_current_operator(monkeypatch, "alice")
    with pytest.raises(ValueError, match="inaccessible"):
        retrieve_original(f"knowledge:{priv['code']}")
    assert retrieve_original(f"knowledge:{shared['code']}")["content"] == "shared body"


@pytest.fixture
def original_rows(isolated_brains, tmp_path):
    from brains.control.operators import add_operator, ensure_admin_operator
    from brains.storage.models import Artifact, Chunk, Org, OrgMember, Source

    ensure_admin_operator()
    operator, _ = add_operator("reader")
    workspace_id = _make_workspace(tmp_path / "originals", "originals", visibility="private")
    with db_module.SessionLocal() as session:
        org = session.query(Org).filter(Org.slug == "default").one()
        session.add(OrgMember(org_id=org.id, operator_id=operator["id"], role="member"))
        source = Source(
            workspace_id=workspace_id, source_type="repo", uri=str(tmp_path / "originals")
        )
        session.add(source)
        session.flush()
        artifact = Artifact(
            source_id=source.id,
            path="original.txt",
            title="Original title",
            summary="stored summary",
            metadata_json=json.dumps({"abs_path": str(tmp_path / "outside-source/original.txt")}),
        )
        session.add(artifact)
        session.flush()
        chunk = Chunk(artifact_id=artifact.id, ordinal=0, content="stored chunk")
        session.add(chunk)
        session.commit()
        return source, artifact, chunk


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
def test_retrieve_original_authorized_and_membership_revoked(original_rows, monkeypatch, kind):
    from unittest.mock import Mock

    import brains.control.retrieve as retrieve
    from brains.control.memberships import add_membership, remove_membership

    _, artifact, chunk = original_rows
    row = artifact if kind == "artifact" else chunk
    ref = f"{kind}:{row.id}"
    add_membership("originals", "reader")
    _set_current_operator(monkeypatch, "reader")
    assert retrieve.retrieve_original(ref)["content"] == (
        "stored summary" if kind == "artifact" else "stored chunk"
    )

    remove_membership("originals", "reader")
    read = Mock(side_effect=AssertionError("inaccessible file read"))
    monkeypatch.setattr(retrieve, "_artifact_content", read)
    with pytest.raises(ValueError) as denied:
        retrieve.retrieve_original(ref)
    assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
    read.assert_not_called()


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
@pytest.mark.parametrize("visibility", ["empty", "other-workspace", "other-org", "unowned"])
def test_retrieve_original_denies_outside_scope(
    original_rows, tmp_path, monkeypatch, kind, visibility
):
    from unittest.mock import Mock

    import brains.control.retrieve as retrieve
    from brains.control.memberships import (
        set_workspace_visibility,
        visible_workspace_ids_for_current,
    )
    from brains.storage.models import OrgMember, Source

    source, artifact, chunk = original_rows
    if visibility == "other-workspace":
        _make_workspace(tmp_path / "other", "other")
    elif visibility == "other-org":
        set_workspace_visibility("originals", "shared")
        with db_module.SessionLocal() as session:
            session.query(OrgMember).delete()
            session.commit()
    elif visibility == "unowned":
        with db_module.SessionLocal() as session:
            session.get(Source, source.id).workspace_id = None
            session.commit()
    _set_current_operator(monkeypatch, "reader")
    visible = visible_workspace_ids_for_current()
    assert visible if visibility == "other-workspace" else visible == set()
    read = Mock(side_effect=AssertionError("inaccessible file read"))
    monkeypatch.setattr(retrieve, "_artifact_content", read)

    row = artifact if kind == "artifact" else chunk
    for ident in (row.id, 999999):
        ref = f"{kind}:{ident}"
        with pytest.raises(ValueError) as denied:
            retrieve.retrieve_original(ref)
        assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
    read.assert_not_called()


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
@pytest.mark.parametrize("ancestry", ["valid", "unowned", "missing-source", "missing-artifact"])
@pytest.mark.parametrize("operator", ["admin", "reader"])
def test_retrieve_original_requires_ancestry(original_rows, monkeypatch, kind, ancestry, operator):
    from unittest.mock import Mock

    import brains.control.retrieve as retrieve
    from brains.control.memberships import add_membership
    from brains.storage.models import Artifact, Chunk, Source

    source, artifact, chunk = original_rows
    add_membership("originals", "reader")
    _set_current_operator(monkeypatch, operator)
    with db_module.SessionLocal() as session:
        if ancestry == "unowned":
            session.get(Source, source.id).workspace_id = None
        elif ancestry == "missing-source":
            session.get(Artifact, artifact.id).source_id = 999999
        elif ancestry == "missing-artifact":
            session.get(Chunk, chunk.id).artifact_id = 999999
        session.commit()
    ident = artifact.id if kind == "artifact" else chunk.id
    if ancestry == "missing-artifact" and kind == "artifact":
        ident = 999999
    ref = f"{kind}:{ident}"
    if ancestry == "valid" or (ancestry == "unowned" and operator == "admin"):
        assert retrieve.retrieve_original(ref)["content"] == (
            "stored summary" if kind == "artifact" else "stored chunk"
        )
    else:
        read = Mock(side_effect=AssertionError("orphan file read"))
        monkeypatch.setattr(retrieve, "_artifact_content", read)
        with pytest.raises(ValueError) as denied:
            retrieve.retrieve_original(ref)
        assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
        read.assert_not_called()


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
def test_retrieve_original_empty_principal_slot_denies_admin_fallback(
    original_rows, monkeypatch, kind
):
    from unittest.mock import Mock

    import brains.control.retrieve as retrieve
    from brains.authz.resolver import principal_slot

    _, artifact, chunk = original_rows
    ref = f"{kind}:{artifact.id if kind == 'artifact' else chunk.id}"
    _set_current_operator(monkeypatch, "admin")
    read = Mock(side_effect=AssertionError("unauthenticated file read"))
    monkeypatch.setattr(retrieve, "_artifact_content", read)
    with principal_slot(), pytest.raises(ValueError) as denied:
        retrieve.retrieve_original(ref)
    assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
    read.assert_not_called()


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
def test_retrieve_original_resolver_failure_denies_without_disclosure(
    original_rows, monkeypatch, kind
):
    from unittest.mock import Mock

    import brains.authz.resolver as resolver
    import brains.control.retrieve as retrieve

    _, artifact, chunk = original_rows
    ref = f"{kind}:{artifact.id if kind == 'artifact' else chunk.id}"
    _set_current_operator(monkeypatch, "admin")
    resolve = Mock(side_effect=RuntimeError("synthetic-private-resolver-detail"))
    monkeypatch.setattr(resolver, "resolve_local_principal", resolve)
    read = Mock(side_effect=AssertionError("unresolved principal file read"))
    monkeypatch.setattr(retrieve, "_artifact_content", read)
    with pytest.raises(ValueError) as denied:
        retrieve.retrieve_original(ref)
    assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
    assert denied.value.__cause__ is None
    assert denied.value.__context__ is None
    resolve.assert_called_once_with()
    read.assert_not_called()


def test_retrieve_original_artifact_keeps_current_file_and_summary_fallback(
    original_rows, tmp_path, monkeypatch
):
    import brains.control.retrieve as retrieve
    from brains.control.memberships import add_membership

    _, artifact, _ = original_rows
    add_membership("originals", "reader")
    _set_current_operator(monkeypatch, "reader")
    ref = f"artifact:{artifact.id}"
    current_file = tmp_path / "outside-source/original.txt"
    current_file.parent.mkdir()
    current_file.write_text("current file content", encoding="utf-8")
    assert retrieve.retrieve_original(ref) == {
        "ref": ref,
        "kind": "artifact",
        "id": artifact.id,
        "title": artifact.title,
        "path": artifact.path,
        "content": "current file content",
        "metadata": {"source_id": artifact.source_id, "language": None, "size": 0},
    }
    current_file.unlink()
    assert retrieve.retrieve_original(ref)["content"] == "stored summary"


@pytest.mark.parametrize("kind", ["artifact", "chunk"])
def test_retrieve_original_mcp_uses_current_principal(original_rows, monkeypatch, kind):
    import inspect
    from unittest.mock import Mock

    import brains.control.retrieve as retrieve
    from brains.authz.resolver import (
        principal_for_operator_slug,
        principal_slot,
        set_current_principal,
    )
    from brains.control.memberships import add_membership, remove_membership
    from brains.mcp.tools import retrieve_original_tool

    _, artifact, chunk = original_rows
    ref = f"{kind}:{artifact.id if kind == 'artifact' else chunk.id}"
    assert list(inspect.signature(retrieve_original_tool).parameters) == ["ref"]
    assert list(inspect.signature(retrieve.retrieve_original).parameters) == ["ref"]
    _set_current_operator(monkeypatch, "admin")
    add_membership("originals", "reader")
    with principal_slot():
        set_current_principal(principal_for_operator_slug("reader"))
        assert retrieve_original_tool(ref)["content"] == (
            "stored summary" if kind == "artifact" else "stored chunk"
        )
        remove_membership("originals", "reader")
        read = Mock(side_effect=AssertionError("inaccessible file read"))
        monkeypatch.setattr(retrieve, "_artifact_content", read)
        with pytest.raises(ValueError) as denied:
            retrieve_original_tool(ref)
        assert str(denied.value) == f"unknown or inaccessible {kind} ref: {ref}"
        read.assert_not_called()
