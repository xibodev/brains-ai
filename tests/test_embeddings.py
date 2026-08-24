"""Tests for the embeddings + semantic-retrieval layer.

The provider HTTP call and the embedding model are mocked, so these run
without a live Ollama. The integration test exercises the real index ->
embed -> cosine-search path against the test database, using a deterministic
bag-of-keywords fake embedder so ranking is checkable.
"""

from __future__ import annotations

import pytest

from brains.context import embeddings, semantic


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_embed_texts_batch(monkeypatch):
    monkeypatch.setattr(
        "brains.context.embeddings.httpx.post",
        lambda *a, **k: _Resp({"embeddings": [[1.0, 2.0], [3.0, 4.0]]}),
    )
    assert embeddings.embed_texts(["a", "b"], model="m") == [[1.0, 2.0], [3.0, 4.0]]


def test_embed_texts_requires_model(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "embed_model", "")
    with pytest.raises(embeddings.EmbeddingError, match="no embedding model"):
        embeddings.embed_texts(["x"])


def test_embed_texts_empty_input_returns_empty():
    assert embeddings.embed_texts([], model="m") == []


def test_embed_texts_shape_mismatch(monkeypatch):
    monkeypatch.setattr(
        "brains.context.embeddings.httpx.post",
        lambda *a, **k: _Resp({"embeddings": [[1.0, 2.0]]}),
    )
    with pytest.raises(embeddings.EmbeddingError, match="shape mismatch"):
        embeddings.embed_texts(["a", "b"], model="m")


def test_embed_texts_http_error(monkeypatch):
    monkeypatch.setattr(
        "brains.context.embeddings.httpx.post",
        lambda *a, **k: _Resp({"error": "model not found"}, status_code=404),
    )
    with pytest.raises(embeddings.EmbeddingError, match=r"failed \(404\): model not found"):
        embeddings.embed_texts(["a"], model="missing")


def test_pack_unpack_roundtrip():
    vec = [1.0, 2.0, 3.0, -4.0]
    assert embeddings.unpack_vector(embeddings.pack_vector(vec)) == vec


def test_cosine():
    assert embeddings.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert embeddings.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert embeddings.cosine([], [1.0]) == 0.0
    assert embeddings.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_score_blobs_matches_cosine():
    """The batched scorer (numpy fast-path or fallback) must agree with cosine()."""
    query = [0.5, 1.0, -0.25, 2.0]
    vecs = [
        [0.5, 1.0, -0.25, 2.0],  # identical -> 1.0
        [-0.5, -1.0, 0.25, -2.0],  # opposite -> -1.0
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],  # zero vector -> 0.0
    ]
    blobs = [embeddings.pack_vector(v) for v in vecs]
    got = embeddings.score_blobs(query, blobs)
    expected = [embeddings.cosine(query, v) for v in vecs]
    assert len(got) == len(expected)
    for g, e in zip(got, expected, strict=False):
        assert abs(g - e) < 1e-5
    assert abs(got[0] - 1.0) < 1e-5
    assert abs(got[1] + 1.0) < 1e-5
    assert got[3] == 0.0


def test_score_blobs_empty_and_ragged():
    assert embeddings.score_blobs([1.0, 2.0], []) == []
    # ragged / empty blob -> 0.0, others still scored
    blobs = [b"", embeddings.pack_vector([1.0, 2.0])]
    got = embeddings.score_blobs([1.0, 2.0], blobs)
    assert got[0] == 0.0
    assert abs(got[1] - 1.0) < 1e-5


# --- semantic index + search (real DB, fake embedder) --------------------

_VOCAB = ["alpha", "beta", "gamma", "delta"]


def _fake_one(text: str) -> list[float]:
    t = text.lower()
    return [float(t.count(w)) for w in _VOCAB]


def _fake_embed_texts(texts, model=None):
    return [_fake_one(t) for t in texts]


def _fake_embed_text(text, model=None):
    return _fake_one(text)


def test_embed_repo_and_semantic_search(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha alpha alpha about the alpha topic", encoding="utf-8")
    (repo / "b.txt").write_text("beta beta something unrelated to the query", encoding="utf-8")

    result = semantic.embed_repo(str(repo), model="test-embed")
    assert result["embed_model"] == "test-embed"
    assert result["embedded_chunks"] >= 2
    assert result["embed_dim"] == len(_VOCAB)

    hits = semantic.search_repo_semantic(str(repo), "alpha", limit=5)
    assert hits, "expected at least one semantic hit"
    assert hits[0]["rel_path"] == "a.txt"  # alpha file ranks first
    assert hits[0]["score"] >= hits[-1]["score"]


def test_semantic_search_dedupes_by_file(tmp_path, monkeypatch):
    """A multi-chunk file must not crowd out other files: results keep the best
    chunk per file so the limited set covers more distinct files."""
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "deduprepo"
    repo.mkdir()
    # big.txt chunks into several pieces, all alpha-heavy; small.txt is one chunk.
    (repo / "big.txt").write_text(("alpha " * 400).strip(), encoding="utf-8")
    (repo / "small.txt").write_text("alpha beta", encoding="utf-8")

    res = semantic.embed_repo(str(repo), model="test-embed")
    assert res["embedded_chunks"] >= 3  # big.txt split into multiple chunks

    hits = semantic.search_repo_semantic(str(repo), "alpha", limit=5)
    paths = [h["rel_path"] for h in hits]
    assert len(paths) == len(set(paths)), f"results should be one-per-file, got {paths}"
    assert "big.txt" in paths


def test_semantic_search_include_exclude_filter(tmp_path, monkeypatch):
    """Path include/exclude must let a caller surface implementation over docs/tests."""
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "filterrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "impl.py").write_text("alpha alpha alpha implementation", encoding="utf-8")
    (repo / "docs" / "guide.txt").write_text("alpha alpha alpha alpha docs prose", encoding="utf-8")
    (repo / "tests" / "test_impl.py").write_text("alpha alpha test code", encoding="utf-8")
    semantic.embed_repo(str(repo), model="test-embed")

    # exclude docs + tests -> only the implementation file remains
    hits = semantic.search_repo_semantic(
        str(repo), "alpha", limit=10, exclude=["/docs/", "/tests/", "test_"]
    )
    paths = [h["rel_path"].replace("\\", "/") for h in hits]
    assert any("src/impl.py" in p for p in paths)
    assert not any("/docs/" in p or "/tests/" in p or "test_" in p for p in paths)

    # include filter keeps only matching paths
    only_docs = semantic.search_repo_semantic(str(repo), "alpha", limit=10, include=["/docs/"])
    assert only_docs and all("docs/" in h["rel_path"].replace("\\", "/") for h in only_docs)


def test_build_orientation_block(tmp_path, monkeypatch):
    """The inject-ready orientation block is code-only by default and renders
    ranked files + snippets (the operational primitive behind the weak-model win)."""
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "orientrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "impl.py").write_text("alpha alpha implementation here", encoding="utf-8")
    (repo / "docs" / "guide.txt").write_text("alpha alpha alpha prose docs", encoding="utf-8")
    (repo / "tests" / "test_impl.py").write_text("alpha alpha test code here", encoding="utf-8")
    semantic.embed_repo(str(repo), model="test-embed")

    block = semantic.build_orientation_block(str(repo), "alpha", limit=5)
    norm = block.replace("\\", "/")
    assert "## Repo orientation" in block
    assert "src/impl.py" in norm
    assert ".txt" not in block  # docs excluded by default
    assert "guide.txt" not in block
    assert "test_impl.py" not in norm  # tests excluded by default

    # include_docs / include_tests widen the set
    with_docs = semantic.build_orientation_block(str(repo), "alpha", limit=5, include_docs=True)
    assert "guide.txt" in with_docs.replace("\\", "/")
    with_tests = semantic.build_orientation_block(str(repo), "alpha", limit=5, include_tests=True)
    assert "test_impl.py" in with_tests.replace("\\", "/")


def test_build_orientation_block_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")
    monkeypatch.setattr(semantic.settings, "semantic_auto_embed", False)
    repo = tmp_path / "emptyorient"
    repo.mkdir()
    (repo / "a.py").write_text("nothing embedded", encoding="utf-8")
    block = semantic.build_orientation_block(str(repo), "alpha")
    assert block.startswith("<!-- brains orientation unavailable")


def test_path_allowed_helper():
    f = semantic._path_allowed
    assert f("django/forms/models.py", None, ["/docs/", "test_"]) is True
    assert f("docs/ref/models.txt", None, ["/docs/"]) is False
    assert f("a/test_thing.py", None, ["test_"]) is False
    assert f("pkg/core.py", ["/pkg/"], None) is True
    assert f("other/core.py", ["/pkg/"], None) is False
    assert f("src/a.py", None, ["*.txt"]) is True
    assert f("src/a.txt", None, ["*.txt"]) is False


def test_embed_repo_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "c.txt").write_text("gamma gamma content", encoding="utf-8")

    first = semantic.embed_repo(str(repo), model="test-embed")
    second = semantic.embed_repo(str(repo), model="test-embed")
    assert first["embedded_chunks"] >= 1
    # nothing changed -> re-embed skips the unchanged file
    assert second["embedded_chunks"] == 0
    assert second["skipped_unchanged"] >= 1


def test_semantic_search_empty_when_nothing_embedded(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")
    repo = tmp_path / "repo3"
    repo.mkdir()
    (repo / "d.txt").write_text("delta", encoding="utf-8")
    # No embed_repo call -> no chunks -> empty result (no crash).
    assert semantic.search_repo_semantic(str(repo), "delta") == []


def test_semantic_status_reports_miss_with_hint(tmp_path, monkeypatch):
    """A miss must NOT be a silent [] — it returns a status + actionable hint so the
    agent doesn't burn a turn then grep while believing brains had nothing."""
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")
    repo = tmp_path / "repo_miss"
    repo.mkdir()
    (repo / "e.txt").write_text("epsilon", encoding="utf-8")
    status = semantic.semantic_search_with_status(str(repo), "epsilon")
    assert status["results"] == []
    assert status["status"] == "workspace_not_indexed"
    assert "embed-repo" in status["hint"]


def test_semantic_auto_embeds_on_first_search(tmp_path, monkeypatch):
    """A search on an un-indexed repo should auto-embed (local model) and return
    results — making 'retrieve, don't grep' actually work without a manual step."""
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")
    monkeypatch.setattr(semantic.settings, "semantic_auto_embed", True)

    repo = tmp_path / "auto_embed_repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha alpha alpha about the alpha topic", encoding="utf-8")

    # NO embed_repo() call — the search should bootstrap it.
    status = semantic.semantic_search_with_status(str(repo), "alpha")
    assert status["status"] == "auto_embedded"
    assert status["results"], "expected auto-embed to produce searchable chunks"
    assert status["results"][0]["rel_path"] == "a.txt"


def test_semantic_auto_embed_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")
    monkeypatch.setattr(semantic.settings, "semantic_auto_embed", False)

    repo = tmp_path / "no_auto_embed_repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha alpha alpha", encoding="utf-8")

    status = semantic.semantic_search_with_status(str(repo), "alpha")
    assert status["results"] == []
    assert "embed-repo" in status["hint"]


def test_semantic_resolves_related_workspace(tmp_path, monkeypatch):
    """Embeddings indexed under a subdir must be found when searching from the repo
    root (and the status flags the auto-resolution) — fixes the workspace-keying
    silent-miss footgun."""
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    repo = tmp_path / "proj"
    src = repo / "src"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("alpha alpha alpha about the alpha topic", encoding="utf-8")

    semantic.embed_repo(str(src), model="test-embed")

    # search from the PARENT (repo root) — exact workspace has no source, but the
    # child src/ index should be resolved.
    status = semantic.semantic_search_with_status(str(repo), "alpha")
    assert status["status"] == "resolved_related"
    assert status["results"], "expected the child src/ index to be resolved"
    assert "hint" in status


def test_chunk_text_windows():
    assert semantic.chunk_text("") == []
    assert semantic.chunk_text("short") == ["short"]
    pieces = semantic.chunk_text("x" * 2500, max_chars=1000, overlap=100)
    assert len(pieces) >= 3
    assert all(len(p) <= 1000 for p in pieces)


def test_search_semantic_mcp_tool_registered():
    """`search_semantic` is experimental: absent from the default advertised
    surface, present once BRAINS_MCP_EXPERIMENTAL opts in."""
    import os

    from brains.mcp.server import _resolve_active_tools, list_tools

    assert "brains_search_semantic" not in list_tools()
    assert "search_semantic" not in set(_resolve_active_tools())
    os.environ["BRAINS_MCP_EXPERIMENTAL"] = "1"
    try:
        assert "search_semantic" in set(_resolve_active_tools())
    finally:
        os.environ.pop("BRAINS_MCP_EXPERIMENTAL", None)


def test_search_semantic_mcp_tool_wraps(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(semantic, "embed_text", _fake_embed_text)
    monkeypatch.setattr(semantic.settings, "embed_model", "test-embed")

    from brains.mcp import tools as mcp_tools

    repo = tmp_path / "mcprepo"
    repo.mkdir()
    (repo / "f.txt").write_text("alpha alpha alpha", encoding="utf-8")
    semantic.embed_repo(str(repo), model="test-embed")

    out = mcp_tools.search_semantic_tool(query="alpha", path=str(repo))
    assert out["query"] == "alpha"
    assert out["results"] and out["results"][0]["rel_path"] == "f.txt"


def test_search_semantic_mcp_tool_requires_query():
    from brains.mcp import tools as mcp_tools

    with pytest.raises(ValueError, match="query is required"):
        mcp_tools.search_semantic_tool()
