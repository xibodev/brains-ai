"""Rung 0 — coordination propagation proof.

The smallest honest test of the brains thesis: a fact one CLI session posts to
the coordination plane reaches a *different* CLI session, automatically at
connect (push) and on demand (pull). No LLM, no network — just the real MCP
tool-dispatch path (`brains.mcp.server.call_tool`), local SQLite, one operator,
two sessions standing in for two agentic CLIs (Copilot "backend" + Claude "UI")
working the same repo.

Scenario (mirrors the brains-core / brains-ui split we want to dogfood):

  Session A (copilot, backend) posts three kinds of process knowledge:
    1. a CAVEAT via knowledge_add   ("never count()+1 — concurrency")
    2. a MESSAGE via send_message   (an API-contract heads-up to the workspace)
    3. a PATTERN via propose/approve (a reusable convention scoped to the repo)

  Session B (claude, UI) connects via start_session and must:
    - SEE all three surfaced in its welcome packet (push), then
    - PULL the actual bodies (knowledge_search / read_messages / use_pattern).

Any failure exits non-zero so this doubles as a CI-style gate. Run sealed:

  docker run --rm -v "${repo}:/work:ro" -e BRAINS_STATE_DIR=/tmp/brains-state \
    -e HOME=/tmp --entrypoint sh brains-dev:multica -c \
    "cp -r /work /tmp/build && cd /tmp/build && pip install -q -e '.[dev]' \
     && python sandbox/collab/rung0_propagation.py"
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Ensure the brain's state dir exists before any DB access. The standalone
# harness (unlike the pytest suite) has no conftest to provision it.
_STATE_DIR = os.environ.get("BRAINS_STATE_DIR", "/tmp/brains-state")
os.makedirs(_STATE_DIR, exist_ok=True)

from brains.mcp.server import call_tool  # noqa: E402

# tiny assertion + transcript helpers
_FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f"  - {detail}"
    print(line, flush=True)
    if not condition:
        _FAILURES.append(label)


def banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="rung0-repo-")
    Path(workspace).mkdir(exist_ok=True)

    CAVEAT_TITLE = "Mint coded rows with insert_with_code_retry, never count()+1"
    CAVEAT_NEEDLE = "insert_with_code_retry"
    MSG_SUBJECT = "API contract: /v1/squads returns slug, not numeric id"
    MSG_NEEDLE = "slug"
    PATTERN_NAME = "squad-routing-tag"

    # Session A: copilot / backend posts process knowledge
    banner("Session A (copilot - backend) posts to the coordination plane")
    a = call_tool("brains_start_session", workspace_path=workspace, tool="copilot")
    a_sid = a["session_id"]
    print(f"  session A = {a_sid} (operator {a['operator']})", flush=True)

    call_tool(
        "brains_knowledge_add",
        workspace_path=workspace,
        type="caveat",
        title=CAVEAT_TITLE,
        body=(
            "Concurrent writers on shared Postgres collide on the unique code "
            "index. Use common.insert_with_code_retry + next_sequential_code; "
            "never count()+1."
        ),
        scope="workspace",
        tags="db,concurrency",
    )
    print("  A: knowledge_add(caveat) ok", flush=True)

    call_tool(
        "brains_send_message",
        subject=MSG_SUBJECT,
        body="UI should key squad cards on slug; ids are internal only.",
        from_session_id=a_sid,
        workspace_path=workspace,
        kind="info",
    )
    print("  A: send_message(workspace) ok", flush=True)

    call_tool(
        "brains_propose_pattern",
        name=PATTERN_NAME,
        category="convention",
        description="Tag squad-routed tasks 'squad:<slug>' so the leader delegates.",
        example="tags='squad:frontend'",
        applies_to=Path(workspace).name,
        session_id=a_sid,
    )
    call_tool("brains_approve_pattern", name=PATTERN_NAME, approved=True)
    print("  A: propose_pattern + approve_pattern ok", flush=True)

    call_tool("brains_end_session", session_id=a_sid, summary="backend caveats posted")

    # Session B: claude / UI connects and must inherit A's knowledge
    banner("Session B (claude - UI) connects - welcome packet (PUSH)")
    b = call_tool("brains_start_session", workspace_path=workspace, tool="claude")
    b_sid = b["session_id"]
    welcome = b.get("welcome") or {}
    print(f"  session B = {b_sid} (operator {b['operator']})", flush=True)

    knowledge = welcome.get("knowledge") or {}
    k_titles = [e.get("title", "") for e in knowledge.get("entries", [])]
    check(
        "welcome surfaces A's caveat",
        any(t == CAVEAT_TITLE for t in k_titles),
        f"knowledge.count={knowledge.get('count')} titles={k_titles}",
    )

    mail = welcome.get("unread_messages") or {}
    check(
        "welcome surfaces A's message",
        any(s == MSG_SUBJECT for s in mail.get("subjects", [])),
        f"unread={mail.get('count')} subjects={mail.get('subjects')}",
    )

    pat_names = [p.get("name", "") for p in welcome.get("applicable_patterns", [])]
    check(
        "welcome surfaces A's pattern",
        PATTERN_NAME in pat_names,
        f"applicable_patterns={pat_names}",
    )

    check(
        "welcome emits actionable hints",
        bool(welcome.get("hints")),
        f"hints={welcome.get('hints')}",
    )

    # Session B pulls the actual bodies (PULL)
    banner("Session B pulls the bodies it was told about (PULL)")
    found = call_tool(
        "brains_knowledge_search", workspace_path=workspace, query="count", status="active"
    )
    bodies = " ".join(str(r) for r in found)
    check(
        "knowledge_search returns A's caveat body",
        CAVEAT_NEEDLE in bodies,
        f"{len(found)} hit(s)",
    )

    inbox = call_tool("brains_read_messages", session_id=b_sid)
    inbox_text = " ".join(str(m) for m in inbox)
    check(
        "read_messages returns A's message body",
        MSG_NEEDLE in inbox_text,
        f"{len(inbox)} message(s)",
    )

    used = call_tool("brains_use_pattern", name=PATTERN_NAME, session_id=b_sid)
    check("use_pattern resolves A's pattern", bool(used), f"{used}")

    call_tool("brains_end_session", session_id=b_sid, summary="inherited backend context")

    # verdict
    banner("VERDICT")
    if _FAILURES:
        print(f"  RUNG 0 FAILED - {len(_FAILURES)} check(s): {_FAILURES}", flush=True)
        return 1
    print("  RUNG 0 PASSED - A's caveat, message and pattern all reached B.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
