from __future__ import annotations

import hashlib
import os
from pathlib import Path

from typer.testing import CliRunner

import brains.context.lookup as lookup_module
from brains.cli import app as cli_app
from brains.context.lookup import MAX_RESULTS, lookup_workspace
from brains.mcp.tools import search_repo_tool
from brains.wire import RULE_BODY


def _tree_fingerprint(root: Path) -> list[tuple[str, int, str]]:
    return [
        (
            path.relative_to(root).as_posix(),
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def test_fresh_repo_lookup_is_ranked_numbered_bounded_and_has_no_side_effects(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "z.py").write_text("noise = 'Widget'\n", encoding="utf-8")
    (root / "a.py").write_text("# context\nclass Widget:\n    pass\n", encoding="utf-8")
    before = _tree_fingerprint(root)

    result = lookup_workspace(root, "Widget", limit=999)
    repeated = lookup_workspace(root, "Widget", limit=999)

    assert result["status"] == "ok"
    assert result["reason"] == "matches_found"
    assert result["results"][0] == {
        "path": "a.py",
        "line": 2,
        "end_line": 3,
        "snippet": "1: # context\n2: class Widget:\n3:     pass",
        "symbol": "Widget",
        "match": "symbol",
    }
    assert len(result["results"]) <= MAX_RESULTS
    assert repeated == result
    assert result["incomplete_reasons"] == []
    assert _tree_fingerprint(root) == before
    assert not (root / ".brains").exists()


def test_empty_and_unavailable_are_distinct_and_errors_do_not_disclose_paths(tmp_path):
    root = tmp_path / "private-name"
    root.mkdir()
    (root / "source.py").write_text("answer = 42\n", encoding="utf-8")

    empty = lookup_workspace(root, "missing symbol")
    unavailable = lookup_workspace(tmp_path / "private-missing", "answer")

    assert (empty["status"], empty["reason"]) == ("empty", "no_matches")
    assert (unavailable["status"], unavailable["reason"]) == (
        "unavailable",
        "root_missing",
    )
    assert "private" not in repr(unavailable)
    assert empty["incomplete_reasons"] == unavailable["incomplete_reasons"] == []


def test_unreadable_root_is_a_non_disclosing_unavailable_state(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    original = os.scandir

    def denied(path):
        if Path(path) == root.resolve():
            raise PermissionError("sensitive operating-system detail")
        return original(path)

    monkeypatch.setattr("brains.context.lookup.os.scandir", denied)
    result = lookup_workspace(root, "query")
    assert (result["status"], result["reason"]) == ("unavailable", "root_unreadable")
    assert "sensitive" not in repr(result)


def test_symlinks_cannot_escape_workspace(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("class EscapedSecret:\n    pass\n", encoding="utf-8")
    try:
        (root / "linked.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    result = lookup_workspace(root, "EscapedSecret")
    assert result["status"] == "empty"


def test_cli_and_mcp_use_the_identical_control_envelope(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "one.ts").write_text("export function lookupTarget() {}\n", encoding="utf-8")
    expected = lookup_workspace(root, "lookupTarget", limit=3)
    assert search_repo_tool(query="lookupTarget", repo_path=str(root), limit=3) == expected

    captured: list[dict] = []
    monkeypatch.setattr(cli_app, "_print_json", captured.append)
    cli_app.search_repo_cli("lookupTarget", str(root), 3)
    assert captured == [expected]


def test_query_required_uses_contract_instead_of_adapter_exception(tmp_path):
    direct = lookup_workspace(tmp_path, "")
    mcp = search_repo_tool(query="", repo_path=str(tmp_path))
    assert direct == mcp
    assert direct["status"] == "unavailable"
    assert direct["reason"] == "query_required"


def test_every_scan_cap_reports_limited_instead_of_no_matches(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("nothing here\n", encoding="utf-8")
    (root / "b.py").write_text("target = True\n", encoding="utf-8")

    monkeypatch.setattr(lookup_module, "MAX_FILES", 1)
    files = lookup_workspace(root, "target")
    assert files["status"] == "limited"
    assert files["reason"] == "file_limit_reached"
    assert files["results"] == []

    monkeypatch.setattr(lookup_module, "MAX_FILES", 2_000)
    monkeypatch.setattr(lookup_module, "MAX_DIRECTORIES", 1)
    nested = root / "nested"
    nested.mkdir()
    (nested / "target.py").write_text("target = True\n", encoding="utf-8")
    directories = lookup_workspace(root, "target")
    assert directories["status"] == "limited"
    assert "directory_limit_reached" in directories["incomplete_reasons"]

    monkeypatch.setattr(lookup_module, "MAX_DIRECTORIES", 500)
    monkeypatch.setattr(lookup_module, "MAX_TOTAL_BYTES", 4)
    byte_limited = lookup_workspace(root, "target")
    assert byte_limited["status"] == "limited"
    assert byte_limited["reason"] == "byte_limit_reached"

    monkeypatch.setattr(lookup_module, "MAX_TOTAL_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(lookup_module, "MAX_FILE_BYTES", 4)
    size_limited = lookup_workspace(root, "target")
    assert size_limited["status"] == "limited"
    assert size_limited["reason"] == "file_size_limit_reached"


def test_result_limit_reports_partial_even_after_a_complete_scan(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("target = 1\ntarget = 2\n", encoding="utf-8")
    result = lookup_workspace(root, "target", limit=1)
    assert result["status"] == "limited"
    assert result["reason"] == "result_limit_reached"
    assert len(result["results"]) == 1


def test_file_read_failure_returns_partial_and_never_empty(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    readable = root / "a.py"
    denied = root / "b.py"
    readable.write_text("target = True\n", encoding="utf-8")
    denied.write_text("other target = True\n", encoding="utf-8")
    original = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == denied:
            raise PermissionError("private path detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = lookup_workspace(root, "target")
    assert result["status"] == "limited"
    assert result["reason"] == "file_unreadable"
    assert result["results"][0]["path"] == "a.py"
    assert "private" not in repr(result)


def test_nested_traversal_failure_returns_limited_not_empty(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("nothing here\n", encoding="utf-8")

    def broken_walk(path, *, topdown, onerror, followlinks):
        yield str(path), [], ["source.py"]
        onerror(PermissionError("private subtree detail"))

    monkeypatch.setattr(lookup_module.os, "walk", broken_walk)
    result = lookup_workspace(root, "target")
    assert result["status"] == "limited"
    assert result["reason"] == "traversal_unreadable"
    assert result["results"] == []
    assert "private" not in repr(result)


def test_non_text_eligible_file_is_incomplete_not_no_match(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "damaged.py").write_bytes(b"\xff\x00target")
    result = lookup_workspace(root, "target")
    assert result["status"] == "limited"
    assert result["reason"] == "file_not_text"
    assert result["results"] == []


def test_default_wire_and_supported_tool_description_recommend_only_local_lookup():
    guidance = RULE_BODY.casefold()
    assert "brains_knowledge_search" in guidance
    assert "brains_search_repo" in guidance
    assert all(word not in guidance for word in ("semantic", "graph", "embed"))
    description = (search_repo_tool.__doc__ or "").casefold()
    assert "substring/symbol" in description
    assert all(word not in description for word in ("semantic", "graph", "embed"))

    help_result = CliRunner().invoke(cli_app.app, ["search-repo", "--help"])
    assert help_result.exit_code == 0, help_result.output
    help_text = help_result.output.casefold()
    assert "substring/symbol" in help_text
    assert all(word not in help_text for word in ("semantic", "graph", "embed"))


def test_modern_browser_handles_every_lookup_state():
    source = (
        Path(__file__).parents[1] / "frontend" / "src" / "screens" / "Workspaces.tsx"
    ).read_text(encoding="utf-8")
    assert "operatorWorkspaceLookup" in source
    assert 'lookup?.status === "ok"' in source
    assert 'lookup?.status === "empty"' in source
    assert 'lookup?.status === "unavailable"' in source
    assert 'lookup?.status === "limited"' in source
    assert "row.path}:{row.line}" in source
    assert "AbortController" in source
    assert "isCurrent(currentWorkspace, requestedWorkspace)" in source
    assert "key={detail.workspace.slug}" in source
    assert "workspace.workspace.slug === selectedSlug" in source
