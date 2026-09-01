"""Seed one disposable container-only browser-test database and print its manifest."""

from __future__ import annotations

import json

from brains.control.decisions import file_decision_request
from brains.control.durable_mail import send_mailbox_message
from brains.control.durable_mailbox import register_agent_mailbox
from brains.control.operators import ensure_admin_operator
from brains.control.orgs import create_org, get_org
from brains.control.sessions import register_workspace, start_session
from brains.storage.migrations import init_db

WORKSPACE_PATH = "/data/e2e-workspace"


def main() -> None:
    init_db()
    ensure_admin_operator()
    org = get_org("demo") or create_org("demo", "Demo Org")
    workspace = register_workspace(
        WORKSPACE_PATH,
        slug="e2e-workspace",
        name="E2E Workspace",
        org_id=int(org["id"]),
    )
    approval = file_decision_request(
        WORKSPACE_PATH,
        title="[gate] approve sealed E2E action",
        body="This is a local, simulated approval used to prove the browser governance loop.",
        proposed_answer="approve",
        metadata={"kind": "action_gate"},
    )

    def agent(tool: str) -> tuple[dict, dict, str]:
        binding = f"e2e-mailbox-binding-{tool}-" + ("x" * 32)
        native_id = f"e2e-{tool}-mailbox-session"
        started = start_session(WORKSPACE_PATH, tool=tool)
        mailbox = register_agent_mailbox(
            WORKSPACE_PATH,
            tool,
            native_id,
            started["session_id"],
            binding,
        )
        return started, mailbox, binding

    sender, sender_mailbox, sender_binding = agent("opencode")
    _other, other_mailbox, _other_binding = agent("codex")
    root = send_mailbox_message(
        WORKSPACE_PATH,
        ["operator:admin@brains"],
        "Mailbox journey handoff",
        "e2e-mailbox-root-v1",
        body="The durable context survived while the operator was away.",
        sender_session_id=sender["session_id"],
        binding_secret=sender_binding,
    )
    print(
        json.dumps(
            {
                "workspace": {
                    "id": workspace.id,
                    "slug": workspace.slug,
                    "path": workspace.path,
                },
                "approval": approval,
                "mailbox": {
                    "workspace": sender["workspace"],
                    "sender_session_id": sender["session_id"],
                    "sender_address": sender_mailbox["address"],
                    "other_address": other_mailbox["address"],
                    "message_id": root["message_id"],
                    "thread_id": root["thread_id"],
                    "subject": root["subject"],
                },
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
