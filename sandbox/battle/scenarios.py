"""Brains-v2 distributed battle-test harness (Layer A).

Runs INSIDE a brains container (alice@brain-a or bob@brain-b) against the
shared Postgres. Each step prints exactly one machine-readable result line:

    RESULT <PASS|FAIL> <step> | <detail>

and may print `OUT key=value` lines the host orchestrator captures to thread
ids between steps/containers. Operator identity comes from BRAINS_OPERATOR;
machine identity from this container's /home/agent/.brains/machine-id.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import timedelta

from brains.control import (
    help as help_mod,
)

# Import after env is in place (container already has BRAINS_* set).
from brains.control import (
    knowledge,
    learn,
    mailbox,
    memberships,
    operators,
    presence,
    sessions,
    signals,
    tasks,
)
from brains.control.common import utc_now
from brains.storage import db as dbmod
from brains.storage.migrations import init_db
from brains.storage.models import AgentSession

SHARED = "/work/shared"
ALICE_PRIV = "/work/alice-private"


def _ok(step, detail=""):
    print(f"RESULT PASS {step} | {detail}")


def _fail(step, detail=""):
    print(f"RESULT FAIL {step} | {detail}")


def _out(key, value):
    print(f"OUT {key}={value}")


def _me():
    return operators.resolve_current_operator()["slug"]


# ---------------------------------------------------------------- setup
def setup_operator():
    """Create the operator row matching BRAINS_OPERATOR (idempotent)."""
    slug = (os.environ.get("BRAINS_OPERATOR") or "").strip().lower()
    if not slug:
        return _fail("setup_operator", "BRAINS_OPERATOR unset")
    existing = {o["slug"] for o in operators.list_operators()}
    if slug in existing:
        _out("operator", slug)
        return _ok("setup_operator", f"{slug} already present")
    try:
        rec, _key = operators.add_operator(slug, display_name=slug.capitalize())
    except operators.OperatorExistsError:
        _out("operator", slug)
        return _ok("setup_operator", f"{slug} exists (race)")
    _out("operator", rec["slug"])
    _ok("setup_operator", f"created {rec['slug']} id={rec['id']}")


# ---------------------------------------------------------------- A1
def a1_distributed_schema():
    init_db()
    mid = sessions.current_machine_id()
    with dbmod.SessionLocal() as s:
        n = s.execute(
            __import__("sqlalchemy").text(
                "select count(*) from information_schema.tables where table_schema='public'"
            )
        ).scalar()
    _out("machine_id", mid)
    if n and int(n) >= 25:
        _ok("a1_schema", f"machine={mid[:12]} pg_tables={n}")
    else:
        _fail("a1_schema", f"only {n} tables")


# ---------------------------------------------------------------- A2
def a2_two_operators():
    slugs = sorted(o["slug"] for o in operators.list_operators())
    if "alice" in slugs and "bob" in slugs:
        _ok("a2_operators", f"operators={slugs}")
    else:
        _fail("a2_operators", f"operators={slugs} (need alice+bob)")


# ---------------------------------------------------------------- A3
def a3_setup_private():
    """alice: register shared + private workspace, flip private."""
    sessions.register_workspace(SHARED, name="shared")
    sessions.register_workspace(ALICE_PRIV, name="alice-private")
    memberships.set_workspace_visibility(ALICE_PRIV, "private")
    vis = sessions.get_workspace(path=ALICE_PRIV).id
    _out("alice_priv_id", vis)
    _ok("a3_setup_private", f"private workspace id={vis}")


def a3_check_visibility():
    """bob: must NOT see alice-private until invited."""
    bob_id = operators.resolve_current_operator()["id"]
    priv = sessions.get_workspace(path=ALICE_PRIV).id
    shared = sessions.get_workspace(path=SHARED).id
    visible = memberships.visible_workspace_ids(bob_id)
    # None == admin sentinel (sees all). bob is non-admin -> a real set.
    if visible is None:
        return _fail("a3_visibility", "bob resolved as admin (set sentinel None)")
    sees_priv = priv in visible
    sees_shared = shared in visible
    if (not sees_priv) and sees_shared:
        _ok("a3_visibility", f"bob sees shared={sees_shared}, private={sees_priv} (correct)")
    else:
        _fail("a3_visibility", f"bob sees shared={sees_shared}, private={sees_priv}")


def a3_invite_and_recheck():
    """alice invites bob to private; harness reruns bob check separately."""
    memberships.add_membership(ALICE_PRIV, "bob", role="member")
    _ok("a3_invite", "bob added to alice-private")


def a3_check_after_invite():
    bob_id = operators.resolve_current_operator()["id"]
    priv = sessions.get_workspace(path=ALICE_PRIV).id
    visible = memberships.visible_workspace_ids(bob_id)
    if visible is not None and priv in visible:
        _ok("a3_post_invite", "bob now sees alice-private")
    else:
        _fail("a3_post_invite", f"bob still cannot see private (visible={visible})")


# ---------------------------------------------------------------- A4
def a4_alice_open_and_task():
    s = sessions.start_session(SHARED, tool="claude", pid=os.getpid())
    sid = s["session_id"]
    t = tasks.create_task(
        SHARED, "Battle: build feature X", body="cross-machine task", session_id=sid
    )
    _out("alice_sid", sid)
    _out("task_code", t["code"])
    _ok("a4_alice_task", f"alice sid={sid} created task {t['code']}")


def a4_bob_claim(task_code):
    s = sessions.start_session(SHARED, tool="copilot", pid=os.getpid())
    sid = s["session_id"]
    tasks.claim_task(task_code, sid)
    _out("bob_sid", sid)
    _ok("a4_bob_claim", f"bob sid={sid} claimed {task_code}")


def a4_bob_handoff(task_code, bob_sid):
    h = tasks.handoff_task(
        task_code,
        title="Battle: review feature X",
        session_id=bob_sid,
        body="bob done implementing, alice please review",
        completion_summary="impl complete",
    )
    new_code = h["next"]["code"]
    old_status = h["completed"]["status"]
    _out("handoff_task_code", new_code)
    _ok("a4_bob_handoff", f"{task_code}->{old_status}, new task {new_code}")


def a4_mailbox_send(alice_sid, bob_sid):
    mailbox.send_message(
        "Battle: heads up",
        body="alice -> bob across machines",
        from_session_id=alice_sid,
        to_session_id=bob_sid,
        kind="info",
    )
    _ok("a4_mail_send", "alice sent message to bob session")


def a4_mailbox_read(bob_sid):
    msgs = mailbox.read_messages(bob_sid)
    hit = [m for m in msgs if "heads up" in (m.get("subject") or "").lower()]
    if hit:
        _ok("a4_mail_read", f"bob read {len(msgs)} msg(s), found alice's")
    else:
        _fail("a4_mail_read", f"bob saw {len(msgs)} msgs, alice's missing")


# ---------------------------------------------------------------- A5 reaper
def a5_own_machine_dead_pid():
    """Own-machine session with a dead PID must be reaped from this machine."""
    s = sessions.start_session(SHARED, tool="claude", pid=999999)
    sid = s["session_id"]
    reaped = sessions.reap_zombie_sessions()
    if sid in reaped:
        _ok("a5_own_dead_pid", f"own-machine dead-pid session {sid} reaped")
    else:
        _fail("a5_own_dead_pid", f"{sid} NOT reaped (reaped={reaped})")


def a5_foreign_fresh_not_reaped(foreign_sid):
    """A FRESH foreign-machine session must survive a reap from here."""
    init_db()
    with dbmod.SessionLocal() as s:
        row = s.query(AgentSession).filter(AgentSession.id == foreign_sid).one_or_none()
        if row is None:
            return _fail("a5_foreign_fresh", f"{foreign_sid} not found")
        row.last_activity_at = utc_now()
        s.commit()
    reaped = sessions.reap_zombie_sessions()
    if foreign_sid not in reaped:
        _ok("a5_foreign_fresh", f"fresh foreign session {foreign_sid} preserved")
    else:
        _fail("a5_foreign_fresh", "fresh foreign session wrongly reaped")


def a5_foreign_stale_reaped(foreign_sid):
    """A STALE foreign-machine session (past TTL) must be reaped from here."""
    init_db()
    with dbmod.SessionLocal() as s:
        row = s.query(AgentSession).filter(AgentSession.id == foreign_sid).one_or_none()
        if row is None:
            return _fail("a5_foreign_stale", f"{foreign_sid} not found")
        row.ended_at = None
        row.last_activity_at = utc_now() - timedelta(
            seconds=sessions.STALE_SESSION_TTL_SECONDS + 120
        )
        s.commit()
    reaped = sessions.reap_zombie_sessions()
    if foreign_sid in reaped:
        _ok("a5_foreign_stale", f"stale foreign session {foreign_sid} reaped")
    else:
        _fail("a5_foreign_stale", f"stale foreign session NOT reaped (reaped={reaped})")


# ---------------------------------------------------------------- A6 peer-help
def a6_alice_ask():
    """Blocking ask_peer; bob answers within the window (run as background)."""
    s = sessions.start_session(SHARED, tool="claude", pid=os.getpid())
    res = help_mod.ask_peer(
        "Battle: how to seed?",
        "What's the canonical seed path?",
        from_session_id=s["session_id"],
        to_workspace="shared",
        context="battle test",
        timeout_ms=25000,
    )
    _out("help_code", res.get("code"))
    if res.get("status") == "answered" and res.get("answer"):
        _ok("a6_ask_answered", f"alice got answer: {res.get('answer')[:40]!r}")
    else:
        _fail("a6_ask_answered", f"status={res.get('status')} answer={res.get('answer')!r}")


def a6_bob_answer():
    """bob: claim the open request (wait_for_request) then answer it."""
    s = sessions.start_session(SHARED, tool="copilot", pid=os.getpid())
    sid = s["session_id"]
    req = help_mod.wait_for_request(session_id=sid, workspace_slug="shared", timeout_ms=12000)
    if not req:
        return _fail("a6_bob_answer", "no request routed to bob within window")
    code = req["code"]
    help_mod.answer_request(
        code,
        answer="Use the canonical brains seed CLI",
        evidence="docs/OPERATIONS.md",
        session_id=sid,
    )
    _ok("a6_bob_answer", f"bob claimed+answered {code}")


# ---------------------------------------------------------------- A7 knowledge
def a7_alice_knowledge():
    e = knowledge.add_knowledge_entry(
        SHARED,
        "caveat",
        "Dockerfile used dropped brains binary",
        body="must use brains-ai",
        confidence="high",
        severity="warn",
        evidence="commit bb17c4b",
        provenance="extracted",
        importance=0.8,
    )
    _out("know_code", e["code"])
    # one that is already expired
    past = (utc_now() - timedelta(days=1)).isoformat()
    e2 = knowledge.add_knowledge_entry(
        SHARED,
        "environment_note",
        "ephemeral note",
        body="should expire",
        valid_until=past,
        provenance="inferred",
    )
    _out("know_expire_code", e2["code"])
    _ok("a7_alice_knowledge", f"added {e['code']} + expiring {e2['code']}")


def a7_bob_reads(know_code):
    res = knowledge.search_knowledge(workspace_path=SHARED)
    codes = {r["code"] for r in res}
    if know_code in codes:
        _ok("a7_bob_reads", f"bob sees alice knowledge ({len(res)} entries)")
    else:
        _fail("a7_bob_reads", f"bob missing {know_code}; saw {sorted(codes)}")


def a7_expire():
    n = knowledge.expire_stale_knowledge()
    _ok("a7_expire", f"expired {n} stale entr{'y' if n == 1 else 'ies'}")


def a7_learn_and_signals():
    prop = learn.propose_from_history(workspace_path=SHARED, apply=False, limit=10)
    sig = signals.list_signals(workspace_path=SHARED)
    pc = len(prop.get("proposals", [])) if isinstance(prop, dict) else "?"
    _ok("a7_learn_signals", f"learn proposals={pc}, signals={len(sig)}")


# ---------------------------------------------------------------- A8 code graph
def a8_build_graph():
    from brains.context import code_graph

    # Build over a small in-repo package subdir to keep it fast.
    target = "/opt/brains/src/brains/control"
    res = code_graph.build_code_graph(target)
    nodes = res.get("nodes") if isinstance(res, dict) else None
    _out("graph_ws", target)
    _ok("a8_build_graph", f"built graph nodes={nodes} for {target}")


def a8_read_graph():
    from brains.context import code_graph

    target = "/opt/brains/src/brains/control"
    answer = code_graph.graph_query(target, "what does start_session do")
    if isinstance(answer, str) and answer.strip():
        _ok("a8_read_graph", f"graph_query cross-read ok ({len(answer)} chars)")
    else:
        _fail("a8_read_graph", f"graph_query returned empty: {answer!r}")


# ---------------------------------------------------------------- A9 concurrency
def a9_concurrent_tasks(prefix, n):
    n = int(n)
    s = sessions.start_session(SHARED, tool="codex", pid=os.getpid())
    sid = s["session_id"]
    created, errors = [], []
    lock = threading.Lock()

    def worker(i):
        try:
            t = tasks.create_task(SHARED, f"conc-{prefix}-{i}", session_id=sid)
            with lock:
                created.append(t["code"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    uniq = len(set(created))
    if len(created) == n and uniq == n and not errors:
        _ok("a9_concurrency", f"{prefix}: {n} tasks, {uniq} unique codes, 0 errors")
    else:
        _fail("a9_concurrency", f"{prefix}: created={len(created)} uniq={uniq} errors={errors[:3]}")


# ---------------------------------------------------------------- presence
def presence_check():
    others = presence.list_other_operators_active()
    slugs = sorted(o["operator_slug"] for o in others)
    _ok("presence", f"{_me()} sees other active operators: {slugs}")


DISPATCH = {
    "setup_operator": setup_operator,
    "a1_distributed_schema": a1_distributed_schema,
    "a2_two_operators": a2_two_operators,
    "a3_setup_private": a3_setup_private,
    "a3_check_visibility": a3_check_visibility,
    "a3_invite_and_recheck": a3_invite_and_recheck,
    "a3_check_after_invite": a3_check_after_invite,
    "a4_alice_open_and_task": a4_alice_open_and_task,
    "a4_bob_claim": a4_bob_claim,
    "a4_bob_handoff": a4_bob_handoff,
    "a4_mailbox_send": a4_mailbox_send,
    "a4_mailbox_read": a4_mailbox_read,
    "a5_own_machine_dead_pid": a5_own_machine_dead_pid,
    "a5_foreign_fresh_not_reaped": a5_foreign_fresh_not_reaped,
    "a5_foreign_stale_reaped": a5_foreign_stale_reaped,
    "a6_alice_ask": a6_alice_ask,
    "a6_bob_answer": a6_bob_answer,
    "a7_alice_knowledge": a7_alice_knowledge,
    "a7_bob_reads": a7_bob_reads,
    "a7_expire": a7_expire,
    "a7_learn_and_signals": a7_learn_and_signals,
    "a8_build_graph": a8_build_graph,
    "a8_read_graph": a8_read_graph,
    "a9_concurrent_tasks": a9_concurrent_tasks,
    "presence_check": presence_check,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in DISPATCH:
        print(f"RESULT FAIL dispatch | unknown step {argv[1:]!r}; known={sorted(DISPATCH)}")
        return 2
    try:
        DISPATCH[argv[1]](*argv[2:])
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"RESULT FAIL {argv[1]} | exception {exc!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
