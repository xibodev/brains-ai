"""Tests for the persistent RAG indexer added in PR-3.

Covers:

* ``chunks_meta`` table is provisioned by both ``create_all`` and the
  ``020_rag_chunks_meta`` disk migration, and is recorded in
  ``schema_versions``.
* ``index_repo_persisted`` inserts on first run, marks unchanged files
  as ``unchanged`` on a second run, updates on content change, and
  removes vanished files.
* ``search_repo_persisted`` returns artifacts that match a substring.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import brains.context.repo_indexer as repo_indexer_module
import brains.control.events as events_module
import brains.control.sessions as sessions_module
import brains.storage.db as db_module
import brains.storage.migrations as migrations_module
from brains.context.repo_indexer import (
    REPO_SOURCE_TYPE,
    index_repo_persisted,
    search_repo_persisted,
)
from brains.storage.migrations import current_schema_versions, init_db
from brains.storage.models import Artifact, Source


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "rag.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (
        db_module,
        migrations_module,
        sessions_module,
        events_module,
        repo_indexer_module,
    ):
        if hasattr(module, "engine"):
            monkeypatch.setattr(module, "engine", engine)
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", SessionLocal)
    yield db_path


def _columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_chunks_meta_provisioned_on_init(isolated_db) -> None:
    init_db()
    cols = _columns(isolated_db, "chunks_meta")
    assert {"id", "embed_model", "embed_dim", "updated_at"}.issubset(cols)
    assert "020_rag_chunks_meta" in current_schema_versions()


def test_index_repo_persisted_first_run_inserts_all(isolated_db, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)")
    (repo / "b.py").write_text("print(2)")

    result = index_repo_persisted(str(repo))
    assert result["inserted"] == 2
    assert result["updated"] == 0
    assert result["unchanged"] == 0
    assert result["removed"] == 0


def test_index_repo_persisted_unchanged_on_second_run(isolated_db, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print(1)")
    index_repo_persisted(str(repo))
    second = index_repo_persisted(str(repo))
    assert second["unchanged"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 0


def test_index_repo_persisted_updates_changed_and_removes_deleted(isolated_db, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("v1")
    (repo / "b.py").write_text("v1")
    index_repo_persisted(str(repo))
    (repo / "a.py").write_text("v2-different")
    (repo / "b.py").unlink()
    second = index_repo_persisted(str(repo))
    assert second["updated"] == 1
    assert second["removed"] == 1


def test_search_repo_persisted_finds_substring(isolated_db, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "alpha.py").write_text("MAGIC_TOKEN_X = 1")
    (repo / "beta.py").write_text("other content")
    index_repo_persisted(str(repo))
    hits = search_repo_persisted(str(repo), "MAGIC_TOKEN_X")
    assert len(hits) == 1
    assert hits[0]["rel_path"] == "alpha.py"


def test_search_repo_persisted_empty_when_not_indexed(isolated_db, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("hello")
    # No prior index_repo_persisted call.
    assert search_repo_persisted(str(repo), "hello") == []


def test_repo_source_row_is_workspace_scoped(isolated_db, tmp_path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "x.py").write_text("alpha")
    (repo_b / "y.py").write_text("beta")
    index_repo_persisted(str(repo_a))
    index_repo_persisted(str(repo_b))

    with db_module.SessionLocal() as session:
        sources = session.query(Source).filter(Source.source_type == REPO_SOURCE_TYPE).all()
        assert len(sources) == 2
        for src in sources:
            artifacts = session.query(Artifact).filter(Artifact.source_id == src.id).all()
            assert len(artifacts) == 1
