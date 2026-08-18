import pytest

from brains.context.planner import plan
from brains.control.claims import (
    claim_workspace,
    list_workspace_claims,
    release_workspace,
)
from brains.control.mailbox import read_messages, send_message
from brains.control.sessions import start_session
from brains.control.snapshots import capture_snapshot, latest_snapshot
from brains.control.tasks import (
    claim_task,
    complete_task,
    create_task,
    list_tasks,
)
from brains.router.classifier import classify


def test_task_claim_conflict_and_completion(tmp_path):
    workspace = str(tmp_path)
    first = start_session(workspace, tool="pytest")
    second = start_session(workspace, tool="pytest")
    task = create_task(workspace, title="Coordinate test task", priority="p1")

    claimed = claim_task(task["code"], first["session_id"])
    assert claimed["status"] == "in_progress"
    assert claimed["claimed_by_session_id"] == first["session_id"]

    with pytest.raises(ValueError, match="claimed by"):
        claim_task(task["code"], second["session_id"])

    completed = complete_task(task["code"], first["session_id"], summary="done")
    assert completed["status"] == "done"
    assert completed["completion_summary"] == "done"
    assert any(row["code"] == task["code"] for row in list_tasks(workspace, status="done"))


def test_task_dependency_blocks_claim_until_done(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    dependency = create_task(workspace, title="Dependency")
    dependent = create_task(
        workspace,
        title="Dependent",
        depends_on=dependency["code"],
    )

    with pytest.raises(ValueError, match="Dependency|dependency"):
        claim_task(dependent["code"], session["session_id"])

    claim_task(dependency["code"], session["session_id"])
    complete_task(dependency["code"], session["session_id"])
    claimed = claim_task(dependent["code"], session["session_id"])
    assert claimed["status"] == "in_progress"


def test_workspace_claim_conflict_release_and_expiry(tmp_path):
    workspace = str(tmp_path)
    first = start_session(workspace, tool="pytest")
    second = start_session(workspace, tool="pytest")

    claim = claim_workspace(workspace, first["session_id"], scope="code")
    assert claim["session_id"] == first["session_id"]

    with pytest.raises(ValueError, match="claimed by"):
        claim_workspace(workspace, second["session_id"], scope="code")

    released = release_workspace(workspace, first["session_id"])
    assert released["released"] is True
    replacement = claim_workspace(workspace, second["session_id"], scope="docs")
    assert replacement["session_id"] == second["session_id"]

    release_workspace(workspace, second["session_id"])
    claim_workspace(workspace, first["session_id"], duration_minutes=-1)
    assert list_workspace_claims(workspace) == []


def test_workspace_claim_concurrent_insert_degrades_gracefully(tmp_path, monkeypatch):
    """A claim that loses a true TOCTOU race must surface the same graceful
    "claimed by X" ValueError as the sequential path — never a raw
    IntegrityError leaked from the workspace_id primary-key collision.

    We reproduce the race deterministically: the winner's claim is committed
    first, then the loser's existence check is forced to miss it exactly once,
    so the loser proceeds to INSERT and collides on the primary key. The fix
    must catch that collision and re-query the committed winner.
    """
    from sqlalchemy.orm import Query

    from brains.storage.models import WorkspaceClaim

    workspace = str(tmp_path)
    winner = start_session(workspace, tool="pytest")
    loser = start_session(workspace, tool="pytest")

    won = claim_workspace(workspace, winner["session_id"], scope="code")
    assert won["session_id"] == winner["session_id"]

    real_one_or_none = Query.one_or_none
    state = {"missed": False}

    def patched_one_or_none(self):
        descriptions = self.column_descriptions
        is_claim_query = bool(descriptions) and descriptions[0].get("entity") is WorkspaceClaim
        if is_claim_query and not state["missed"]:
            # Simulate the concurrent-winner's row being invisible to the
            # loser's existence check (the TOCTOU window). Subsequent claim
            # lookups (the post-collision re-query) see the real row.
            state["missed"] = True
            return None
        return real_one_or_none(self)

    monkeypatch.setattr(Query, "one_or_none", patched_one_or_none)

    with pytest.raises(ValueError, match="claimed by"):
        claim_workspace(workspace, loser["session_id"], scope="code")

    monkeypatch.undo()
    claims = list_workspace_claims(workspace)
    assert len(claims) == 1
    assert claims[0]["session_id"] == winner["session_id"]


def test_mailbox_send_read_and_mark_read(tmp_path):
    workspace = str(tmp_path)
    sender = start_session(workspace, tool="pytest")
    recipient = start_session(workspace, tool="pytest")

    message = send_message(
        subject="Need review",
        body="Please check TASK-0001",
        from_session_id=sender["session_id"],
        to_session_id=recipient["session_id"],
    )
    assert message["subject"] == "Need review"

    unread = read_messages(recipient["session_id"], mark_read=False)
    assert any(row["id"] == message["id"] for row in unread)

    marked = read_messages(recipient["session_id"])
    assert any(row["id"] == message["id"] for row in marked)
    assert read_messages(recipient["session_id"]) == []


def test_snapshot_capture_and_latest(tmp_path):
    workspace = str(tmp_path)
    first = capture_snapshot(workspace, "git", {"branch": "main", "commit": "a"})
    second = capture_snapshot(workspace, "git", {"branch": "main", "commit": "b"})

    latest = latest_snapshot(workspace, "git")
    assert first["id"] != second["id"]
    assert latest is not None
    assert latest["data"]["commit"] == "b"


def test_planner_includes_coordination_signals(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    task = create_task(workspace, title="Available planner task")
    claim_workspace(workspace, session["session_id"], scope="code")

    classification = classify([{"role": "user", "content": "hello"}])
    result = plan(classification, workspace_path=workspace)

    assert any(row["code"] == task["code"] for row in result["coordination"]["available_tasks"])
    assert result["coordination"]["active_claims"]
