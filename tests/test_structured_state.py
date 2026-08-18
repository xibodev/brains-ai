import shutil

from brains.control.claims import claim_workspace
from brains.control.decisions import file_decision_request
from brains.control.handoffs import set_handoff
from brains.control.mailbox import send_message
from brains.control.sessions import start_session
from brains.control.state import get_state
from brains.control.tasks import create_task
from brains.mcp.server import call_tool, list_tools


def test_structured_state_returns_live_coordination_data(tmp_path):
    workspace = str(tmp_path)
    session = start_session(workspace, tool="pytest")
    task = create_task(workspace, title="State task")
    claim_workspace(workspace, session["session_id"], scope="code")
    file_decision_request(workspace, title="State decision")
    set_handoff(workspace, title="State handoff", body="resume")
    message = send_message(
        subject="State message",
        workspace_path=workspace,
        to_session_id=session["session_id"],
    )
    shutil.rmtree(tmp_path / ".brains", ignore_errors=True)

    state = get_state(workspace_path=workspace, session_id=session["session_id"])

    assert state["workspace"]["path"] == workspace
    assert any(row["code"] == task["code"] for row in state["active_tasks"])
    assert state["active_claims"]
    assert state["open_decisions"]
    assert state["active_handoffs"]
    assert any(row["id"] == message["id"] for row in state["unread_messages"])
    assert state["recent_events"]
    assert not (tmp_path / ".brains" / "views").exists()


def test_structured_state_mcp_tool(tmp_path):
    create_task(str(tmp_path), title="MCP state task")

    assert "brains_get_state" in list_tools()
    state = call_tool("brains_get_state", workspace_path=str(tmp_path))

    assert any(row["title"] == "MCP state task" for row in state["active_tasks"])
