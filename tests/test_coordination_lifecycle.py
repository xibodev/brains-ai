from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from brains.control.claims import claim_workspace, list_workspace_claims
from brains.control.handoffs import list_handoffs, pick_handoff, set_handoff
from brains.control.resume import checkpoint, list_checkpoints
from brains.control.session_commands import KIND_MESSAGE, RESULT_UNSUPPORTED, enqueue
from brains.control.sessions import end_session, get_agent_session, set_session_state, start_session
from brains.control.tasks import claim_task, complete_task, create_task, get_task


def _concurrent(count, call):
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


@pytest.mark.parametrize("terminal_path", ["state", "end"])
def test_terminal_transition_releases_ownership_and_cannot_resurrect(tmp_path, terminal_path):
    workspace = str(tmp_path)
    agent = start_session(workspace, tool="pytest")
    session_id = agent["session_id"]
    task = create_task(workspace, "terminal cleanup")
    claim_task(task["code"], session_id)
    claim_workspace(workspace, session_id)

    if terminal_path == "state":
        ended = set_session_state(session_id, "completed", summary="done")
    else:
        end_session(session_id, summary="done")
        ended = get_agent_session(session_id)

    assert ended is not None
    assert ended["state"] == "completed"
    assert ended["ended_at"] is not None
    assert list_workspace_claims(workspace) == []
    released = get_task(task["code"])
    assert released is not None
    assert released["status"] == "available"
    assert released["claimed_by_session_id"] is None
    with pytest.raises(ValueError, match="already ended"):
        set_session_state(session_id, "running")
    with pytest.raises(ValueError, match="ended"):
        claim_task(task["code"], session_id)
    with pytest.raises(ValueError, match="ended"):
        claim_workspace(workspace, session_id)
    with pytest.raises(ValueError, match="ended"):
        checkpoint(session_id, "too late")


def test_task_claim_is_atomic_live_and_workspace_bound(tmp_path):
    workspace = str(tmp_path / "one")
    other_workspace = str(tmp_path / "two")
    agents = [start_session(workspace, tool="pytest") for _ in range(4)]
    outsider = start_session(other_workspace, tool="pytest")
    task = create_task(workspace, "one winner")

    with pytest.raises(ValueError, match="same workspace"):
        claim_task(task["code"], outsider["session_id"])

    def claim(index):
        try:
            return claim_task(task["code"], agents[index]["session_id"])["claimed_by_session_id"]
        except ValueError:
            return None

    winners = [winner for winner in _concurrent(len(agents), claim) if winner]
    assert len(winners) == 1
    stored = get_task(task["code"])
    assert stored is not None
    assert stored["claimed_by_session_id"] == winners[0]

    unclaimed = create_task(workspace, "one completion")

    def complete(index):
        try:
            return complete_task(unclaimed["code"], agents[index]["session_id"])["status"]
        except ValueError:
            return None

    completions = [status for status in _concurrent(len(agents), complete) if status]
    assert completions == ["done"]


def test_workspace_claim_is_live_and_workspace_bound(tmp_path):
    workspace = str(tmp_path / "one")
    other_workspace = str(tmp_path / "two")
    agents = [start_session(workspace, tool="pytest") for _ in range(4)]
    outsider = start_session(other_workspace, tool="pytest")

    with pytest.raises(ValueError, match="must match"):
        claim_workspace(workspace, outsider["session_id"])

    def claim(index):
        try:
            return claim_workspace(workspace, agents[index]["session_id"])["session_id"]
        except ValueError:
            return None

    winners = [winner for winner in _concurrent(len(agents), claim) if winner]
    assert len(winners) == 1
    assert list_workspace_claims(workspace)[0]["session_id"] == winners[0]


def test_handoff_set_and_pick_are_single_winner_operations(tmp_path):
    workspace = str(tmp_path)
    agents = [start_session(workspace, tool="pytest") for _ in range(4)]

    def publish(index):
        return set_handoff(
            workspace,
            f"candidate {index}",
            session_id=agents[index]["session_id"],
        )

    _concurrent(len(agents), publish)
    active = list_handoffs(workspace)
    assert len(active) == 1

    def pick(index):
        try:
            return pick_handoff(workspace, agents[index]["session_id"])["handoff_id"]
        except ValueError:
            return None

    winners = [winner for winner in _concurrent(len(agents), pick) if winner]
    assert winners == [active[0]["handoff_id"]]
    assert list_handoffs(workspace) == []


def test_checkpoint_retry_is_atomic_and_rejects_ended_session(tmp_path):
    workspace = str(tmp_path)
    agent = start_session(workspace, tool="pytest")
    session_id = agent["session_id"]

    results = _concurrent(
        4,
        lambda _index: checkpoint(
            session_id,
            "same checkpoint",
            next_action="continue",
            metadata={"stable": True},
        ),
    )

    assert len({result["id"] for result in results}) == 1
    assert sum(not result["duplicate"] for result in results) == 1
    assert len(list_checkpoints(session_id)) == 1
    set_session_state(session_id, "failed", summary="stopped")
    with pytest.raises(ValueError, match="ended"):
        checkpoint(session_id, "after failure")


def test_reuse_survives_concurrency_and_process_restart(tmp_path):
    workspace = str(tmp_path)

    def register(_index):
        return start_session(workspace, tool="pytest", reuse_existing=True)["session_id"]

    session_ids = _concurrent(6, register)
    assert len(set(session_ids)) == 1
    session_id = session_ids[0]

    task = create_task(workspace, "durable task")
    claim_task(task["code"], session_id)
    claim_workspace(workspace, session_id)
    set_handoff(workspace, "durable handoff", session_id=session_id)
    checkpoint(session_id, "durable checkpoint", next_action="resume")
    command, created = enqueue(
        session_id,
        KIND_MESSAGE,
        text="unsupported delivery proof",
        operation_id="coordination-lifecycle-unsupported",
    )
    assert created is True
    assert command["result"] == RESULT_UNSUPPORTED

    script = (
        "import json; from brains.control.sessions import start_session; "
        f"print(json.dumps(start_session({workspace!r}, tool='pytest', reuse_existing=True)))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        text=True,
    )
    resumed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert resumed["session_id"] == session_id
    assert resumed["reused"] is True
    assert get_task(task["code"])["claimed_by_session_id"] == session_id
    assert list_workspace_claims(workspace)[0]["session_id"] == session_id
    assert list_handoffs(workspace)[0]["title"] == "durable handoff"
    assert list_checkpoints(session_id)[0]["summary"] == "durable checkpoint"
    assert get_agent_session(session_id)["state"] == "running"
