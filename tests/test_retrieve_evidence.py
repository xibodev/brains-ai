"""Evidence provenance and authorization regressions using synthetic local files."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import brains.control.retrieve as retrieve
import brains.storage.db as db_module
from brains.authz.principal import Principal
from brains.storage.models import Artifact, Base, Chunk, KnowledgeEntry, Source, Workspace


@pytest.fixture
def evidence_db(tmp_path, monkeypatch):
    """Only the retrieval tables; no registration, indexing, or ambient state."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'evidence.sqlite').as_posix()}")
    tables = [model.__table__ for model in (Workspace, Source, Artifact, Chunk, KnowledgeEntry)]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(retrieve, "init_db", lambda: None)
    from brains.authz import policy, resolver

    monkeypatch.setattr(policy, "init_db", lambda: None)
    monkeypatch.setattr(policy, "default_org_id", lambda: 101)
    principal = Principal(
        actor_kind="operator",
        actor_id="synthetic-admin",
        credential_kind="admin",
        is_bootstrap_admin=True,
    )
    monkeypatch.setattr(resolver, "resolve_local_principal", lambda: principal)
    yield factory
    engine.dispose()


@pytest.fixture
def indexed(evidence_db, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    source_root = root / "docs"
    source_root.mkdir()
    path = source_root / "guide.md"
    payload = b"# Captured guide\nOriginal UTF-8 evidence.\n"
    path.write_bytes(payload)
    with evidence_db() as session:
        workspace = Workspace(slug="synthetic-workspace", path=str(root), org_id=101)
        session.add(workspace)
        session.flush()
        source = Source(workspace_id=workspace.id, source_type="docs_dir", uri=str(source_root))
        session.add(source)
        session.flush()
        artifact = Artifact(
            source_id=source.id,
            path="guide.md",
            title="Guide",
            summary="Indexed summary",
            language="markdown",
            hash=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        session.add(artifact)
        session.flush()
        chunk = Chunk(
            artifact_id=artifact.id, ordinal=0, content="Captured guide", hash=artifact.hash
        )
        session.add(chunk)
        session.commit()
    return {
        "root": root,
        "source_root": source_root,
        "path": path,
        "payload": payload,
        "workspace": workspace,
        "source": source,
        "artifact": artifact,
        "chunk": chunk,
    }


def _update(factory, model, row_id, **values):
    with factory() as session:
        row = session.get(model, row_id)
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()


def _artifact(indexed):
    return retrieve.retrieve_original(f"artifact:{indexed['artifact'].id}")


def _no_file_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("inaccessible reference attempted filesystem access")

    monkeypatch.setattr(retrieve, "_candidate", forbidden)
    monkeypatch.setattr(retrieve, "_read_regular", forbidden)


def test_full_file_hash_matches_without_claiming_immutable_capture(indexed):
    result = _artifact(indexed)
    assert result["content"] == indexed["payload"].decode()
    assert {
        "ref",
        "kind",
        "id",
        "title",
        "path",
        "content",
        "metadata",
        "evidence",
    } == result.keys()
    assert result["path"] == "guide.md"
    assert result["metadata"]["source_id"] == indexed["source"].id
    assert result["metadata"]["language"] == "markdown"
    assert result["metadata"]["size"] == len(indexed["payload"])
    evidence = result["evidence"]
    assert evidence["origin"] == "current_file"
    assert evidence["freshness"] == "current"
    assert evidence["original_verified"] is True
    assert evidence["immutable_original"] is False
    assert evidence["snapshot"] is True
    assert evidence["expected_hash"] == evidence["observed_hash"] == indexed["artifact"].hash
    assert evidence["hash_scope"] == "full_file"
    assert evidence["truncated"] is evidence["incomplete"] is False
    for key in ("origin", "freshness", "original_verified", "immutable_original", "snapshot"):
        assert result["metadata"][key] == evidence[key]


def test_changed_file_returns_current_snapshot_with_stale_index_evidence(indexed):
    changed = b"Changed since indexing.\n"
    indexed["path"].write_bytes(changed)
    result = _artifact(indexed)
    assert result["content"] == changed.decode()
    assert result["evidence"]["freshness"] == "stale"
    assert result["evidence"]["reason"] == "hash_mismatch"
    assert result["evidence"]["expected_hash"] == indexed["artifact"].hash
    assert result["evidence"]["observed_hash"] == hashlib.sha256(changed).hexdigest()
    assert result["evidence"]["original_verified"] is False


@pytest.mark.parametrize("digest", [None, "", "not-a-sha256", "sha256:bad"])
def test_missing_or_legacy_hash_never_verifies(indexed, evidence_db, digest):
    _update(evidence_db, Artifact, indexed["artifact"].id, hash=digest)
    result = _artifact(indexed)
    assert result["evidence"]["freshness"] == "unknown"
    assert result["evidence"]["original_verified"] is False
    assert result["evidence"]["expected_hash"] is None
    assert result["evidence"]["observed_hash"] == indexed["artifact"].hash


@pytest.mark.parametrize("summary,origin", [("Indexed summary", "summary"), (None, "missing")])
def test_deleted_file_reports_missing_reason_and_explicit_fallback(
    indexed, evidence_db, summary, origin
):
    indexed["path"].unlink()
    _update(evidence_db, Artifact, indexed["artifact"].id, summary=summary)
    result = _artifact(indexed)
    assert result["content"] == (summary or "")
    assert result["evidence"]["origin"] == origin
    assert result["evidence"]["reason"] == "file_missing"
    assert result["evidence"]["freshness"] == "stale"
    assert result["evidence"]["incomplete"] is True
    assert result["evidence"]["original_verified"] is False


@pytest.mark.parametrize("payload", [b"text\x00binary", b"\xff\xfeinvalid UTF-8"])
def test_binary_file_is_not_returned_as_replacement_text(indexed, payload):
    indexed["path"].write_bytes(payload)
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert result["evidence"]["origin"] == "summary"
    assert result["evidence"]["reason"] == "binary_file"
    assert result["evidence"]["original_verified"] is False


def test_unreadable_file_has_distinct_fallback(indexed, monkeypatch):
    def denied(*args):
        raise PermissionError("sensitive local OS detail")

    monkeypatch.setattr(retrieve, "_read_regular", denied)
    result = _artifact(indexed)
    assert result["evidence"]["reason"] == "file_unreadable"
    assert result["evidence"]["origin"] == "summary"
    assert "sensitive" not in json.dumps(result)


def test_oversized_utf8_read_is_bounded_and_cannot_verify_full_file(
    indexed, evidence_db, monkeypatch
):
    cap = retrieve.CONTENT_LIMIT_BYTES
    payload = b"a" * (cap - 1) + "€".encode() + b"z" * cap
    indexed["path"].write_bytes(payload)
    _update(evidence_db, Artifact, indexed["artifact"].id, hash=hashlib.sha256(payload).hexdigest())
    reads = []
    actual_read = os.read

    def counted(fd, count):
        value = actual_read(fd, count)
        reads.append(len(value))
        return value

    monkeypatch.setattr(os, "read", counted)
    result = _artifact(indexed)
    assert sum(reads) == cap + 1
    assert result["content"] == "a" * (cap - 1)
    assert result["evidence"]["truncated"] is result["evidence"]["incomplete"] is True
    assert result["evidence"]["freshness"] == "unknown"
    assert result["evidence"]["expected_hash"] == hashlib.sha256(payload).hexdigest()
    assert result["evidence"]["observed_hash"] is None
    assert result["evidence"]["original_verified"] is False


def test_exact_cap_complete_file_can_verify(indexed, evidence_db):
    payload = b"a" * retrieve.CONTENT_LIMIT_BYTES
    indexed["path"].write_bytes(payload)
    _update(evidence_db, Artifact, indexed["artifact"].id, hash=hashlib.sha256(payload).hexdigest())
    evidence = _artifact(indexed)["evidence"]
    assert evidence["original_verified"] is True
    assert evidence["truncated"] is False


@pytest.mark.parametrize("source_type,uri", [("repo_dir", "docs"), ("docs_dir", "docs")])
def test_relative_source_root_is_workspace_relative(indexed, evidence_db, source_type, uri):
    _update(evidence_db, Source, indexed["source"].id, source_type=source_type, uri=uri)
    assert _artifact(indexed)["content"] == indexed["payload"].decode()


@pytest.mark.parametrize("source_type", ["repo_dir", "docs_dir"])
@pytest.mark.parametrize("absolute", [False, True])
def test_workspace_root_source_uses_indexer_relative_artifact_paths(
    indexed, evidence_db, source_type, absolute
):
    _update(
        evidence_db,
        Source,
        indexed["source"].id,
        source_type=source_type,
        uri=str(indexed["root"]) if absolute else ".",
    )
    _update(
        evidence_db,
        Artifact,
        indexed["artifact"].id,
        path=indexed["path"].relative_to(indexed["root"]).as_posix(),
    )
    result = _artifact(indexed)
    assert result["content"] == indexed["payload"].decode()
    assert result["evidence"]["original_verified"] is True


def test_metadata_abs_path_does_not_expand_source_authority(indexed, evidence_db, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    _update(
        evidence_db,
        Artifact,
        indexed["artifact"].id,
        metadata_json=json.dumps({"abs_path": str(outside)}),
    )
    assert _artifact(indexed)["content"] == indexed["payload"].decode()
    indexed["path"].unlink()
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert "outside secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "escape", ["parent", "absolute_workspace", "absolute_source", "windows_parent"]
)
def test_artifact_cannot_escape_either_root(indexed, evidence_db, tmp_path, monkeypatch, escape):
    sibling = indexed["root"] / "sibling.md"
    sibling.write_text("workspace sibling", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside workspace", encoding="utf-8")
    paths = {
        "parent": "../sibling.md",
        "absolute_workspace": str(outside),
        "absolute_source": str(sibling),
        "windows_parent": "..\\sibling.md",
    }
    _update(evidence_db, Artifact, indexed["artifact"].id, path=paths[escape])
    monkeypatch.setattr(retrieve, "_read_regular", lambda *args: pytest.fail("unsafe file opened"))
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert result["evidence"]["reason"] == "unsafe_path"


def test_absolute_artifact_inside_both_roots_remains_compatible(indexed, evidence_db):
    _update(evidence_db, Artifact, indexed["artifact"].id, path=str(indexed["path"]))
    assert _artifact(indexed)["evidence"]["original_verified"] is True


def test_source_uri_outside_registered_workspace_is_refused(
    indexed, evidence_db, tmp_path, monkeypatch
):
    outside = tmp_path / "other-source"
    outside.mkdir()
    (outside / "guide.md").write_text("unauthorized source", encoding="utf-8")
    _update(evidence_db, Source, indexed["source"].id, uri=str(outside))
    monkeypatch.setattr(
        retrieve, "_read_regular", lambda *args: pytest.fail("unsafe source opened")
    )
    assert _artifact(indexed)["evidence"]["reason"] == "unsafe_path"


@pytest.mark.parametrize("uri", ["../outside", "docs/../docs", "docs\\..\\docs"])
def test_source_uri_parent_traversal_is_refused(indexed, evidence_db, monkeypatch, uri):
    _update(evidence_db, Source, indexed["source"].id, uri=uri)
    monkeypatch.setattr(
        retrieve, "_read_regular", lambda *args: pytest.fail("traversing source opened")
    )
    assert _artifact(indexed)["evidence"]["reason"] == "unsafe_path"


@pytest.mark.parametrize("component", ["file", "directory", "source", "workspace"])
def test_symlink_components_are_refused_before_open(
    indexed, evidence_db, tmp_path, monkeypatch, component
):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "guide.md"
    target.write_text("must never read", encoding="utf-8")
    link = indexed["source_root"] / "link"
    try:
        if component == "file":
            link.symlink_to(target)
            _update(evidence_db, Artifact, indexed["artifact"].id, path="link")
        elif component == "directory":
            link.symlink_to(outside, target_is_directory=True)
            _update(evidence_db, Artifact, indexed["artifact"].id, path="link/guide.md")
        elif component == "source":
            link = indexed["root"] / "source-link"
            link.symlink_to(outside, target_is_directory=True)
            _update(evidence_db, Source, indexed["source"].id, uri=str(link))
        else:
            link = tmp_path / "workspace-link"
            link.symlink_to(indexed["root"], target_is_directory=True)
            _update(evidence_db, Workspace, indexed["workspace"].id, path=str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this platform")
    monkeypatch.setattr(retrieve, "_read_regular", lambda *args: pytest.fail("symlink opened"))
    assert _artifact(indexed)["evidence"]["reason"] == "unsafe_path"


def test_directory_is_not_opened_as_file(indexed, evidence_db, monkeypatch):
    (indexed["source_root"] / "folder").mkdir()
    _update(evidence_db, Artifact, indexed["artifact"].id, path="folder")
    monkeypatch.setattr(retrieve, "_read_regular", lambda *args: pytest.fail("directory opened"))
    assert _artifact(indexed)["evidence"]["reason"] == "not_regular_file"


def test_fifo_is_refused_without_blocking(indexed, evidence_db, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    fifo = indexed["source_root"] / "pipe"
    os.mkfifo(fifo)
    _update(evidence_db, Artifact, indexed["artifact"].id, path="pipe")
    monkeypatch.setattr(retrieve, "_read_regular", lambda *args: pytest.fail("FIFO opened"))
    assert _artifact(indexed)["evidence"]["reason"] == "not_regular_file"


def test_remote_source_returns_summary_without_local_or_network_fetch(
    indexed, evidence_db, monkeypatch
):
    _update(
        evidence_db,
        Source,
        indexed["source"].id,
        source_type="url",
        uri="https://example.invalid/guide.md",
    )
    import httpx

    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("HTTP request"))
    monkeypatch.setattr(
        retrieve, "_read_regular", lambda *args: pytest.fail("remote source opened")
    )
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert result["evidence"]["reason"] == "unsupported_source"


@pytest.mark.parametrize("kind", ["chunk", "artifact"])
@pytest.mark.parametrize(
    "ancestry", ["missing_artifact", "missing_source", "missing_workspace", "null_workspace"]
)
def test_missing_ancestry_denied_before_file_access(
    indexed, evidence_db, monkeypatch, kind, ancestry
):
    from brains.control import memberships

    monkeypatch.setattr(
        memberships, "visible_workspace_ids_for_current", lambda: {indexed["workspace"].id}
    )
    if ancestry == "missing_artifact":
        with evidence_db() as session:
            session.delete(session.get(Artifact, indexed["artifact"].id))
            session.commit()
    elif ancestry == "missing_source":
        _update(evidence_db, Artifact, indexed["artifact"].id, source_id=99999)
    else:
        _update(
            evidence_db,
            Source,
            indexed["source"].id,
            workspace_id=None if ancestry == "null_workspace" else 99999,
        )
    _no_file_access(monkeypatch)
    with pytest.raises(ValueError, match="unknown or inaccessible"):
        retrieve.retrieve_original(f"{kind}:{indexed[kind].id}")


@pytest.mark.parametrize("ancestry", ["missing_source", "missing_workspace"])
def test_bootstrap_admin_still_requires_existing_ancestry(
    indexed, evidence_db, monkeypatch, ancestry
):
    if ancestry == "missing_source":
        _update(evidence_db, Artifact, indexed["artifact"].id, source_id=99999)
    else:
        _update(evidence_db, Source, indexed["source"].id, workspace_id=99999)
    _no_file_access(monkeypatch)
    with pytest.raises(ValueError, match="unknown or inaccessible"):
        _artifact(indexed)


def test_bootstrap_can_read_stored_unscoped_evidence_but_not_unregistered_files(
    indexed, evidence_db, monkeypatch
):
    _update(evidence_db, Source, indexed["source"].id, workspace_id=None)
    monkeypatch.setattr(
        retrieve, "_read_regular", lambda *args: pytest.fail("unregistered root opened")
    )
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert result["evidence"]["reason"] == "unscoped_source"
    chunk = retrieve.retrieve_original(f"chunk:{indexed['chunk'].id}")
    assert chunk["content"] == "Captured guide"


@pytest.mark.parametrize("kind", ["chunk", "artifact"])
@pytest.mark.parametrize(
    "mode", ["empty", "other_org", "private", "resolution_failure", "policy_failure"]
)
def test_current_policy_denies_without_revealing_metadata(
    indexed, evidence_db, monkeypatch, kind, mode
):
    from brains.authz import policy, resolver

    def failed(*args):
        raise RuntimeError("private policy failure")

    principal = Principal(
        actor_kind="operator",
        actor_id="synthetic-reader",
        credential_kind="operator",
        org_roles={} if mode == "empty" else {202 if mode == "other_org" else 101: "member"},
    )
    monkeypatch.setattr(
        resolver,
        "resolve_local_principal",
        failed if mode == "resolution_failure" else lambda: principal,
    )
    if mode == "policy_failure":
        monkeypatch.setattr(policy, "visible_workspace_ids", failed)
    if mode == "private":
        _update(evidence_db, Workspace, indexed["workspace"].id, visibility="private")
    _no_file_access(monkeypatch)
    for row_id in (indexed[kind].id, 99999):
        ref = f"{kind}:{row_id}"
        with pytest.raises(ValueError) as error:
            retrieve.retrieve_original(ref)
        assert str(error.value) == f"unknown or inaccessible ref: {ref}"


def test_matching_current_principal_can_read_registered_source(indexed, monkeypatch):
    from brains.authz import resolver

    principal = Principal(
        actor_kind="operator",
        actor_id="reader",
        credential_kind="operator",
        org_roles={101: "member"},
    )
    monkeypatch.setattr(resolver, "resolve_local_principal", lambda: principal)
    assert _artifact(indexed)["evidence"]["original_verified"] is True


def test_chunk_file_digest_is_not_misused_as_content_checksum(indexed, monkeypatch):
    _no_file_access(monkeypatch)
    result = retrieve.retrieve_original(f"chunk:{indexed['chunk'].id}")
    assert result["content"] == "Captured guide"
    assert result["metadata"]["artifact_id"] == indexed["artifact"].id
    assert result["metadata"]["ordinal"] == 0
    evidence = result["evidence"]
    assert evidence["origin"] == "stored_chunk"
    assert evidence["captured_file_hash"] == indexed["artifact"].hash
    assert evidence["observed_hash"] is None
    assert evidence["freshness"] == "unknown"
    assert evidence["snapshot"] is True
    assert evidence["immutable_original"] is evidence["original_verified"] is False


def test_chunk_reports_newer_index_and_bounds_stored_text(indexed, evidence_db):
    _update(evidence_db, Artifact, indexed["artifact"].id, hash="a" * 64)
    ref = f"chunk:{indexed['chunk'].id}"
    assert retrieve.retrieve_original(ref)["evidence"]["freshness"] == "stale"
    _update(evidence_db, Chunk, indexed["chunk"].id, content="€" * retrieve.CONTENT_LIMIT_BYTES)
    result = retrieve.retrieve_original(ref)
    assert len(result["content"].encode("utf-8")) <= retrieve.CONTENT_LIMIT_BYTES
    assert result["evidence"]["truncated"] is result["evidence"]["incomplete"] is True


def _knowledge(factory, workspace_id, **values):
    with factory() as session:
        row = KnowledgeEntry(
            code="KNOW-0101",
            type="caveat",
            title="Historical evidence",
            workspace_id=workspace_id,
            body="Preserved historical body",
            **values,
        )
        session.add(row)
        session.commit()
        return row


@pytest.mark.parametrize(
    "status,days,freshness",
    [
        ("active", 1, "current"),
        ("confirmed", -1, "expired"),
        ("stale", 1, "stale"),
        ("resolved", 1, "unknown"),
        ("superseded", -1, "superseded"),
    ],
)
def test_knowledge_body_survives_lifecycle_with_provenance(
    indexed, evidence_db, status, days, freshness
):
    row = _knowledge(
        evidence_db,
        indexed["workspace"].id,
        status=status,
        valid_until=datetime.now(UTC) + timedelta(days=days),
        provenance="extracted",
        confidence="high",
        evidence="Recorded citation",
    )
    result = retrieve.retrieve_original(f"knowledge:{row.code}")
    assert result["content"] == result["body"] == row.body
    assert result["metadata"]["stored_status"] == status
    assert result["metadata"]["freshness"] == result["evidence"]["freshness"] == freshness
    assert result["evidence"]["origin"] == "stored_knowledge"
    assert result["evidence"]["recorded_evidence"] == "Recorded citation"
    assert result["evidence"]["provenance"] == "extracted"
    assert result["evidence"]["confidence"] == "high"
    assert result["evidence"]["immutable_original"] is False
    with evidence_db() as session:
        assert session.get(KnowledgeEntry, row.id).status == status


@pytest.mark.parametrize("visible_successor", [False, True])
def test_successor_ref_is_only_exposed_when_visible(
    indexed, evidence_db, monkeypatch, visible_successor
):
    from brains.control import memberships

    row = _knowledge(evidence_db, indexed["workspace"].id)
    with evidence_db() as session:
        successor = KnowledgeEntry(
            code="KNOW-0999",
            type="caveat",
            title="Hidden successor",
            body="Hidden successor body",
            workspace_id=None,
            scope="shared" if visible_successor else "private",
        )
        session.add(successor)
        session.flush()
        old = session.get(KnowledgeEntry, row.id)
        old.superseded_by_id = successor.id
        old.valid_until = datetime.now(UTC) - timedelta(days=1)
        session.commit()
    monkeypatch.setattr(
        memberships, "visible_workspace_ids_for_current", lambda: {indexed["workspace"].id}
    )
    result = retrieve.retrieve_original(f"knowledge:{row.code}")
    assert result["body"] == row.body
    assert result["evidence"]["freshness"] == "superseded"
    assert result["metadata"]["status"] == "superseded"
    assert result["evidence"]["successor_ref"] == (
        "knowledge:KNOW-0999" if visible_successor else None
    )
    assert "superseded_by_id" not in result["metadata"]
    if not visible_successor:
        assert "KNOW-0999" not in json.dumps(result)


def test_knowledge_body_and_recorded_evidence_are_bounded(indexed, evidence_db):
    row = _knowledge(
        evidence_db, indexed["workspace"].id, evidence="€" * retrieve.CONTENT_LIMIT_BYTES
    )
    _update(evidence_db, KnowledgeEntry, row.id, body="€" * retrieve.CONTENT_LIMIT_BYTES)
    result = retrieve.retrieve_original(f"knowledge:{row.code}")
    assert len(result["body"].encode("utf-8")) <= retrieve.CONTENT_LIMIT_BYTES
    assert (
        len(result["evidence"]["recorded_evidence"].encode("utf-8")) <= retrieve.CONTENT_LIMIT_BYTES
    )
    assert result["body"] == result["content"]
    assert result["evidence"]["truncated"] is True
    assert result["evidence"]["recorded_evidence_truncated"] is True


def test_summary_fallback_is_bounded(indexed, evidence_db):
    indexed["path"].unlink()
    _update(
        evidence_db, Artifact, indexed["artifact"].id, summary="€" * retrieve.CONTENT_LIMIT_BYTES
    )
    result = _artifact(indexed)
    assert len(result["content"].encode("utf-8")) <= retrieve.CONTENT_LIMIT_BYTES
    assert result["evidence"]["origin"] == "summary"
    assert result["evidence"]["truncated"] is True


def test_denied_chunk_does_not_load_stored_content_or_metadata(indexed, evidence_db, monkeypatch):
    from sqlalchemy import event

    from brains.control import memberships

    monkeypatch.setattr(memberships, "visible_workspace_ids_for_current", lambda: set())
    engine = evidence_db.kw["bind"]
    selects = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ValueError, match="unknown or inaccessible"):
            retrieve.retrieve_original(f"chunk:{indexed['chunk'].id}")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert selects
    for statement in selects:
        for column in (
            "chunks.content",
            "artifacts.summary",
            "artifacts.metadata_json",
            "sources.uri",
        ):
            assert column not in statement


def test_hidden_knowledge_uses_same_error_as_missing(indexed, evidence_db, monkeypatch):
    from brains.control import memberships

    row = _knowledge(evidence_db, indexed["workspace"].id, scope="private")
    monkeypatch.setattr(memberships, "visible_workspace_ids_for_current", lambda: set())
    for code in (row.code, "KNOW-9999"):
        ref = f"knowledge:{code}"
        with pytest.raises(ValueError) as error:
            retrieve.retrieve_original(ref)
        assert str(error.value) == f"unknown or inaccessible knowledge ref: {ref}"


def test_regular_descriptor_rejects_file_replaced_after_path_check(indexed, monkeypatch):
    path = indexed["path"]
    actual_check = retrieve._check_components
    calls = 0

    def swap_after_check(candidate):
        nonlocal calls
        info = actual_check(candidate)
        calls += 1
        if calls == 1:
            path.rename(path.with_suffix(".old"))
            path.write_text("replacement bytes", encoding="utf-8")
        return info

    monkeypatch.setattr(retrieve, "_check_components", swap_after_check)
    with pytest.raises(retrieve._FileUnavailable, match="file_changed_during_read"):
        retrieve._read_regular(path)


def test_file_changed_during_read_never_verifies_or_returns_mixed_bytes(indexed, monkeypatch):
    actual_read = os.read
    changed = False

    def changing_read(fd, count):
        nonlocal changed
        payload = actual_read(fd, count)
        if not changed:
            changed = True
            with indexed["path"].open("ab") as handle:
                handle.write(b"concurrent append")
        return payload

    monkeypatch.setattr(os, "read", changing_read)
    result = _artifact(indexed)
    assert result["content"] == "Indexed summary"
    assert result["evidence"]["reason"] == "file_changed_during_read"
    assert result["evidence"]["original_verified"] is False


def test_reparse_attribute_is_rejected():
    from types import SimpleNamespace

    info = SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
    assert retrieve._is_link(info) is True
