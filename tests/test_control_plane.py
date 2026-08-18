from brains.context.docs_indexer import index_docs, search_docs
from brains.context.planner import plan
from brains.control.decisions import (
    file_decision_request,
    list_open_decisions,
    resolve_decision,
)
from brains.control.handoffs import pick_handoff, set_handoff
from brains.control.sessions import end_session, start_session
from brains.control.views import refresh_views
from brains.router.classifier import classify


def test_session_decision_handoff_and_views(tmp_path):
    workspace = str(tmp_path)
    started = start_session(workspace, tool="pytest")
    assert started["session_id"].startswith("ses_")

    ask = file_decision_request(
        workspace,
        title="Approve test decision",
        body="body",
        proposed_answer="yes",
        session_id=started["session_id"],
    )
    assert ask["status"] == "open"
    assert any(row["code"] == ask["code"] for row in list_open_decisions(workspace))

    resolved = resolve_decision(ask["code"], chosen="approved")
    assert resolved["status"] == "resolved"

    handoff = set_handoff(
        workspace,
        title="Continue test work",
        body="next step",
        session_id=started["session_id"],
    )
    assert handoff["status"] == "active"
    picked = pick_handoff(workspace, session_id=started["session_id"])
    assert picked["body"] == "next step"

    ended = end_session(started["session_id"], summary="done")
    assert ended["ok"] is True

    views = refresh_views(workspace)
    assert views["ok"] is True
    assert (tmp_path / ".brains" / "views" / "STATE.md").exists()


def test_docs_indexer_prunes_and_persists(tmp_path):
    (tmp_path / "README.md").write_text("# Root\n\nRoot summary.", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "SKIP.md").write_text("# Skip", encoding="utf-8")

    result = index_docs(str(tmp_path))
    assert result["count"] == 1
    assert result["records"][0]["rel_path"] == "README.md"
    matches = search_docs(str(tmp_path), "Root")
    assert matches
    assert matches[0]["path"] == "README.md"


def test_planner_prefers_active_handoff(tmp_path):
    set_handoff(str(tmp_path), title="Resume this", body="handoff body")
    classification = classify([{"role": "user", "content": "Fix bug in file"}])
    result = plan(classification, workspace_path=str(tmp_path))
    assert result["strategy"] == "handoff_resume"
