"""Rung 1 — anti-collision + handoff durability.

Two hardening claims, one rung above the propagation proof:

  1. ANTI-COLLISION. Two sessions cannot both hold the same workspace at once.
     While session A holds a code claim, B's claim is *refused*; once A releases,
     B can claim. This is what stops two agentic CLIs editing the same repo
     simultaneously and clobbering each other.

  2. HANDOFF DURABILITY. A handoff is workspace-scoped, so it survives the death
     of the session that set it. A sets a handoff and ends; a *later* B session
     sees it in its welcome packet and picks it up. This is "resume across tool
     restarts" — context doesn't die with the process.

Deterministic, sealed, no LLM. Drives the real MCP tool-dispatch path. Exits
non-zero on any failed check so it doubles as a gate. Run sealed:

  docker run --rm -v "${repo}:/work:ro" -e BRAINS_STATE_DIR=/tmp/brains-state \
    -e HOME=/tmp --entrypoint sh brains-dev:multica -c \
    "cp -r /work /tmp/build && cd /tmp/build && pip install -q -e '.[dev]' \
     && python sandbox/collab/rung1_anticollision.py"
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_STATE_DIR = os.environ.get("BRAINS_STATE_DIR", "/tmp/brains-state")
os.makedirs(_STATE_DIR, exist_ok=True)

from brains.mcp.server import call_tool  # noqa: E402

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
    repo = tempfile.mkdtemp(prefix="rung1-repo-")
    Path(repo).mkdir(exist_ok=True)

    # ── Part 1: anti-collision on one repo ─────────────────────────────
    banner("Anti-collision: two sessions cannot both hold one repo")
    a = call_tool("brains_start_session", workspace_path=repo, tool="copilot")
    b = call_tool("brains_start_session", workspace_path=repo, tool="claude")
    a_sid, b_sid = a["session_id"], b["session_id"]

    a_claim = call_tool(
        "brains_claim_workspace", workspace_path=repo, session_id=a_sid, scope="code"
    )
    check("A acquires the code claim", bool(a_claim), f"{a_claim.get('scope')}")

    denied = False
    try:
        call_tool("brains_claim_workspace", workspace_path=repo, session_id=b_sid, scope="code")
    except Exception as exc:  # noqa: BLE001 - we WANT the refusal
        denied = "claimed by" in str(exc)
        print(f"  B's competing claim refused: {exc}", flush=True)
    check("B's competing claim is refused while A holds it", denied)

    claims = call_tool("brains_list_workspace_claims", workspace_path=repo)
    holder_is_a = any(c.get("session_id") == a_sid for c in claims)
    check("claim ledger shows A as the holder", holder_is_a, f"{len(claims)} claim(s)")

    call_tool("brains_release_workspace", workspace_path=repo, session_id=a_sid)
    reclaimed = False
    try:
        got = call_tool(
            "brains_claim_workspace", workspace_path=repo, session_id=b_sid, scope="code"
        )
        reclaimed = bool(got)
    except Exception as exc:  # noqa: BLE001
        print(f"  unexpected: B still refused after release: {exc}", flush=True)
    check("B can claim once A releases", reclaimed)
    call_tool("brains_release_workspace", workspace_path=repo, session_id=b_sid)
    call_tool("brains_end_session", session_id=a_sid, summary="claim test done")
    call_tool("brains_end_session", session_id=b_sid, summary="claim test done")

    # ── Part 2: handoff survives the death of the session that set it ───
    banner("Handoff durability: context survives the setting session ending")
    HANDOFF_TITLE = "Resume: squad board needs the open-work badge wired"
    setter = call_tool("brains_start_session", workspace_path=repo, tool="copilot")
    setter_sid = setter["session_id"]
    call_tool(
        "brains_set_handoff",
        workspace_path=repo,
        title=HANDOFF_TITLE,
        body="Backend exposes open_work count; UI must render it on the squad card.",
        session_id=setter_sid,
    )
    print(f"  setter session {setter_sid} set a handoff, then ends", flush=True)
    call_tool("brains_end_session", session_id=setter_sid, summary="handoff left for next session")

    # A brand-new session (the "restarted tool") must inherit the handoff.
    resumer = call_tool("brains_start_session", workspace_path=repo, tool="claude")
    resumer_sid = resumer["session_id"]
    active = resumer.get("active_handoff") or {}
    check(
        "new session's welcome carries the prior handoff",
        active.get("title") == HANDOFF_TITLE,
        f"active_handoff={active.get('title')!r}",
    )

    picked = call_tool("brains_pick_handoff", workspace_path=repo, session_id=resumer_sid)
    check(
        "resumer can pick up the handoff",
        bool(picked),
        f"{picked.get('title') if isinstance(picked, dict) else picked}",
    )

    remaining = call_tool("brains_list_handoffs", workspace_path=repo, active_only=True)
    check(
        "handoff is no longer active once picked",
        not any((h.get("title") == HANDOFF_TITLE) for h in remaining),
        f"{len(remaining)} active handoff(s) remain",
    )
    call_tool("brains_end_session", session_id=resumer_sid, summary="resumed prior context")

    # ── verdict ────────────────────────────────────────────────────────
    banner("VERDICT")
    if _FAILURES:
        print(f"  RUNG 1 FAILED - {len(_FAILURES)} check(s): {_FAILURES}", flush=True)
        return 1
    print("  RUNG 1 PASSED - no double-claim; handoff outlived its session.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
