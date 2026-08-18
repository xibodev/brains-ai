"""Rich, believable seed data for the brains battle dashboards.

Tells one coherent story: a small team (alice on Claude Code, bob on GitHub
Copilot CLI) building a checkout/payments feature in the ``acme-platform``
workspace, coordinating entirely through brains over a shared Postgres.

Run as alice inside brain-a and as bob inside brain-b so sessions/tasks/
knowledge are attributed to the right operator + machine. ``savings`` and
``graph`` and ``patterns`` are global (run once).

    python seed.py alice        # in brain-a (BRAINS_OPERATOR=alice)
    python seed.py bob          # in brain-b (BRAINS_OPERATOR=bob)
    python seed.py patterns     # once
    python seed.py savings      # once
    python seed.py graph        # once
"""

from __future__ import annotations

import contextlib
import sys
from datetime import timedelta

from brains.control import (
    claims,
    decisions,
    handoffs,
    knowledge,
    mailbox,
    patterns,
    sessions,
    tasks,
)
from brains.control.common import utc_now
from brains.control.events import append_event

ACME = "/work/acme-platform"

LONG_BLOCKER_BODY = (
    "Stripe webhook deliveries to POST /webhooks/stripe started returning 400 "
    "with 'No signatures found matching the expected signature for payload'. "
    "Root cause: our FastAPI app sits behind the gateway which re-serialises "
    "the JSON body before it reaches the handler, so the raw bytes Stripe "
    "signed no longer match what we recompute. Stripe's construct_event() "
    "requires the EXACT raw request body, not a parsed-then-redumped dict. "
    "Reproduction: trigger any test event from the Stripe CLI "
    "(`stripe trigger payment_intent.succeeded`) against the UAT URL; the "
    "signature check fails every time even though STRIPE_WEBHOOK_SECRET is "
    "correct. Workaround in place: read request.body() (bytes) inside the "
    "route and pass those bytes straight to construct_event before any "
    "middleware can touch them, and exclude /webhooks/* from the body-"
    "normalising middleware. Permanent fix tracked in the resolution entry: "
    "move webhook verification to an edge function that sees the untouched "
    "payload. Affected files: api/webhooks.py, middleware/normalise.py. "
    "Owners: alice. Severity: payment confirmations silently dropped in UAT, "
    "so orders never transition to 'paid'. Detection: Sentry issue PAY-412 + "
    "a spike in 400s on the webhook route in the gateway access log."
)


def _say(msg):
    print(f"SEED {msg}")


# --------------------------------------------------------------- alice
def seed_alice():
    a1 = sessions.start_session(ACME, tool="claude", pid=1, metadata={"branch": "feat/checkout"})
    a2 = sessions.start_session(ACME, tool="claude", pid=1, metadata={"branch": "feat/refunds"})
    sid1, sid2 = a1["session_id"], a2["session_id"]
    claims.claim_workspace(ACME, sid1, scope="code", duration_minutes=120)
    _say(f"alice sessions {sid1}, {sid2} + workspace baton")

    # Task backlog
    t_design = tasks.create_task(
        ACME,
        "Design checkout API contract",
        body="OpenAPI for /checkout, /charge, /refund",
        priority="p1",
        session_id=sid1,
    )
    tasks.claim_task(t_design["code"], sid1)
    tasks.complete_task(t_design["code"], sid1, summary="Contract agreed; merged to main")

    t_impl = tasks.create_task(
        ACME,
        "Implement Stripe payment intent",
        body="Create + confirm PaymentIntent on /charge",
        priority="p0",
        tags="payments,backend",
        session_id=sid1,
    )
    tasks.claim_task(t_impl["code"], sid1)  # in_progress

    tasks.create_task(
        ACME,
        "Add idempotency keys to /charge",
        body="Prevent double-charge on client retry",
        priority="p1",
        tags="payments",
        session_id=sid1,
    )

    tasks.create_task(
        ACME,
        "Write checkout integration tests",
        body="Cover happy path + card declined + webhook",
        priority="p2",
        depends_on=t_impl["code"],
        tags="testing",
        session_id=sid1,
    )

    t_web = tasks.create_task(
        ACME,
        "Investigate webhook signature failures",
        body="400 on signature mismatch in UAT",
        priority="p1",
        tags="payments,bug",
        session_id=sid2,
    )
    tasks.claim_task(t_web["code"], sid2)  # in_progress
    _say("alice created 5 tasks (1 done, 2 in_progress, 2 available)")

    # Knowledge ledger — blocker (long body), workaround->resolution chain, caveat
    blk = knowledge.add_knowledge_entry(
        ACME,
        "blocker",
        "Stripe webhook 400 on signature mismatch",
        body=LONG_BLOCKER_BODY,
        confidence="high",
        severity="warn",
        evidence="Sentry PAY-412; gateway access log",
        provenance="extracted",
        importance=0.95,
        session_id=sid2,
        scope="shared",
    )
    work = knowledge.add_knowledge_entry(
        ACME,
        "workaround",
        "Pin stripe-python to 9.x to dodge construct_event change",
        body="9.x keeps the old construct_event signature; unblocks UAT today.",
        confidence="medium",
        severity="info",
        provenance="inferred",
        importance=0.5,
        session_id=sid2,
        scope="shared",
    )
    knowledge.add_knowledge_entry(
        ACME,
        "resolution",
        "Upgrade to stripe-python 10 + raw-body webhook verify",
        body="Read request.body() bytes pre-middleware; exclude /webhooks/* from "
        "body normalisation. Closes the 400s permanently.",
        confidence="high",
        severity="info",
        provenance="extracted",
        importance=0.8,
        session_id=sid2,
        scope="shared",
        supersedes_code=work["code"],
    )
    knowledge.add_knowledge_entry(
        ACME,
        "caveat",
        "Use SELECT ... FOR UPDATE SKIP LOCKED for the payouts queue",
        body="Two workers polling payouts will double-pay without SKIP LOCKED.",
        confidence="high",
        severity="warn",
        provenance="extracted",
        importance=0.7,
        session_id=sid1,
        scope="shared",
    )
    _say(f"alice knowledge: {blk['code']} (blocker) + workaround/resolution chain + caveat")

    handoffs.set_handoff(
        ACME,
        "Refunds: next up wire the partial-refund UI",
        body="Backend /refund is in review (see KNOW chain). UI needs the "
        "amount picker + optimistic row update. Branch feat/refunds.",
        session_id=sid2,
    )
    decisions.file_decision_request(
        ACME,
        "Should refunds process sync or async?",
        body="Sync is simpler but ties up the request for up to 8s on Stripe "
        "latency spikes. Async via the payouts queue decouples it but needs "
        "a status webhook back to the client.",
        proposed_answer="async via the payouts queue + status webhook",
        session_id=sid1,
    )
    append_event(
        "decision",
        "Chose Postgres advisory locks over Redis for the payouts mutex (one less moving part).",
        session_id=sid1,
    )
    append_event(
        "work", "Checkout API contract merged; Stripe intent flow 60% done.", session_id=sid1
    )
    _say("alice handoff + open decision + ledger notes")


# --------------------------------------------------------------- bob
def seed_bob():
    b1 = sessions.start_session(
        ACME, tool="copilot", pid=1, metadata={"branch": "feat/idempotency"}
    )
    sid = b1["session_id"]
    _say(f"bob session {sid}")

    # bob picks up an available task alice created
    avail = [t for t in tasks.list_tasks(workspace_path=ACME, status="available")]
    picked = None
    for t in avail:
        if "idempotency" in t["title"].lower():
            picked = t
            break
    if picked:
        tasks.claim_task(picked["code"], sid)  # in_progress
        _say(f"bob claimed {picked['code']} ({picked['title']})")

    # bob finishes the integration-tests task via a handoff back to the team
    tests = [
        t
        for t in tasks.list_tasks(workspace_path=ACME)
        if "integration tests" in t["title"].lower()
    ]
    if tests:
        code = tests[0]["code"]
        with contextlib.suppress(Exception):  # noqa: BLE001 - may already be claimed
            tasks.claim_task(code, sid)
        tasks.handoff_task(
            code,
            title="Review checkout test coverage + add WebKit run",
            session_id=sid,
            body="Happy path + declined card covered. Need a "
            "WebKit lane and a webhook-replay test.",
            completion_summary="Chromium + Firefox lanes green (18 assertions).",
            priority="p2",
            tags="testing",
        )
        _say(f"bob completed {code} and handed off a review task")

    knowledge.add_knowledge_entry(
        ACME,
        "environment_note",
        "Sealed UAT stack: docker compose -f compose.test.yml up",
        body="Brings up app + postgres + mailhog + minio on shifted ports; "
        "Playwright points at http://localhost:18080.",
        confidence="high",
        severity="info",
        provenance="extracted",
        importance=0.6,
        session_id=sid,
        scope="shared",
    )
    knowledge.add_knowledge_entry(
        ACME,
        "dependency_note",
        "minio is required for receipt-PDF storage in UAT",
        body="Without minio the /receipt endpoint 500s; seed a 'receipts' bucket.",
        confidence="medium",
        severity="info",
        provenance="inferred",
        importance=0.4,
        session_id=sid,
        scope="shared",
    )
    _say("bob knowledge: environment + dependency notes")

    # cross-machine mailbox: bob -> alice's first active session
    alice_sessions = [
        s
        for s in sessions.list_sessions(workspace_path=ACME)
        if s.get("tool") == "claude" and s.get("ended_at") is None
    ]
    if alice_sessions:
        mailbox.send_message(
            "Picked up the idempotency task",
            body="Using a Redis-free approach: unique index on (idempotency_key). "
            "Will ping you when /charge is safe to retry.",
            from_session_id=sid,
            to_session_id=alice_sessions[0]["id"],
            kind="info",
        )
        _say("bob mailed alice")
    append_event(
        "fix",
        "Idempotency: added unique index on (idempotency_key); "
        "duplicate POST now returns the original charge.",
        session_id=sid,
    )


# --------------------------------------------------------------- patterns
def seed_patterns():
    specs = [
        (
            "sealed-uat-ports",
            "testing",
            "Run UAT on a sealed Docker network with host ports shifted to 2xxxx so "
            "it can never touch a live local brains install.",
            "ports: 28787/28876/28877 + repo mounted :ro",
        ),
        (
            "retry-on-unique-code",
            "concurrency",
            "For PREFIX-NNNN coded rows on a shared DB, mint via max-suffix and wrap "
            "the insert in insert_with_code_retry so concurrent writers can't collide.",
            "common.insert_with_code_retry(build, finalize)",
        ),
        (
            "compact-context-refs",
            "context",
            "Hand agents compact knowledge:<code> / chunk:<id> refs instead of full "
            "bodies; expand losslessly with retrieve_original only when needed.",
            "search_knowledge(compressed) -> retrieve_original('knowledge:KNOW-0001')",
        ),
    ]
    for name, cat, desc, example in specs:
        try:
            patterns.propose_pattern(name, cat, desc, example=example)
        except Exception as exc:  # noqa: BLE001 - idempotent re-seed
            _say(f"propose {name} skipped: {exc!r}")
        patterns.approve_pattern(name, True)
        _say(f"pattern approved: {name}")


# --------------------------------------------------------------- savings
def seed_savings():
    from brains.router.savings import record_usage

    now = utc_now()
    # (routed_model, provider, endpoint, in, out, n_rows over the week)
    plan = [
        ("gpt-4o-mini", "openai", "/v1/chat/completions", 4200, 380, 11),
        ("claude-3-5-haiku", "anthropic", "/v1/messages", 6100, 520, 9),
        ("claude-haiku-4", "anthropic", "/v1/messages", 5200, 410, 7),
        ("gpt-4o-mini", "openai", "/v1/chat/completions", 8800, 690, 8),
        ("claude-3-haiku", "anthropic", "/v1/messages", 3100, 240, 6),
    ]
    written = 0
    for model, provider, endpoint, itok, otok, count in plan:
        for i in range(count):
            occurred = now - timedelta(days=(written % 7), hours=(i % 12))
            row = record_usage(
                endpoint=endpoint,
                requested_model="gpt-4o",
                routed_model=model,
                provider=provider,
                input_tokens=itok + (i * 37),
                output_tokens=otok + (i * 11),
                task_type="code",
                occurred_at=occurred,
            )
            if row is not None:
                written += 1
    _say(f"savings ledger seeded: {written} routed-call rows (baseline gpt-4o)")


# --------------------------------------------------------------- graph
def seed_graph():
    from brains.context import code_graph

    for target in ("/opt/brains/src/brains/control", "/opt/brains/src/brains/context"):
        res = code_graph.build_code_graph(target)
        _say(f"graph built: {target} -> nodes={res.get('nodes')} edges={res.get('edges')}")


DISPATCH = {
    "alice": seed_alice,
    "bob": seed_bob,
    "patterns": seed_patterns,
    "savings": seed_savings,
    "graph": seed_graph,
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    if which not in DISPATCH:
        print(f"unknown seed target {which!r}; known={sorted(DISPATCH)}")
        sys.exit(2)
    DISPATCH[which]()
