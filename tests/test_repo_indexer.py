from brains.context.repo_indexer import index_repo


def test_repo_indexer_ignores_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "a.txt").write_text("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("print(1)")
    rows = index_repo(str(tmp_path))
    paths = [r["path"] for r in rows]
    assert any("ok.py" in p for p in paths)
    assert all(".git" not in p for p in paths)
