"""Adversarial research panel and conversational work coordination tests (Issue #38 AC2 & AC3).

Validates:
- Independent first-round research with strict blinding to prevent shared conclusion anchoring (AC2)
- Preservation of individual evidence, uncertainty, disagreement, and clarifications through discussion (AC2)
- Structured final synthesis with consensus and open questions returning unresolved dissent to requester (AC2)
- Adversarial research journey across coordinator interruption and restart in both 'rounds' and 'threaded' modes (AC3)
"""

from __future__ import annotations

import socket
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from brains.authz import resolver
from brains.authz.principal import Principal
from brains.control import coordination as peer
from brains.control import session_liveness, sessions
from brains.storage.models import (
    AgentSession,
    Operator,
    Org,
    Workspace,
)

PATH = "/synthetic/conversational-panel"


@pytest.fixture
def panel_world(tmp_path, monkeypatch):
    path = tmp_path / "panel.sqlite"
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"timeout": 15})

    @event.listens_for(engine, "connect")
    def enable_fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(peer, "SessionLocal", factory)
    monkeypatch.setattr(peer, "init_db", lambda: None)
    clock = SimpleNamespace(now=datetime(2030, 1, 1, tzinfo=UTC))
    for module in (peer, sessions, session_liveness):
        monkeypatch.setattr(module, "utc_now", lambda: clock.now)

    # Initialize tables using SQLite baseline migrations
    from brains.authz import policy
    from brains.storage import migrations

    monkeypatch.setattr(policy, "init_db", lambda: None)
    monkeypatch.setattr(policy._db_module, "SessionLocal", factory)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "engine", engine)
        patch.setattr(migrations, "SessionLocal", factory)
        migrations.reset_migration_cache()
        migrations.run_migrations()

    with factory() as session:
        org = session.query(Org).filter(Org.slug == "default").one()
        principal = Principal(
            actor_kind="operator",
            actor_id="operator:synthetic",
            credential_kind="operator",
            operator_id=1,
            channel="browser",
            org_roles={org.id: "owner"},
        )
        session.add(Operator(id=1, slug="owner"))
        session.flush()
        session.add(Workspace(id=1, slug="panel-ws", path=PATH, org_id=org.id))
        session.flush()

        # Specialist sessions: coordinator, researcher_wal (a), researcher_rollback (b)
        for ident, tool in (
            ("coordinator", "codex"),
            ("researcher_wal", "claude-code"),
            ("researcher_rollback", "opencode"),
        ):
            session.add(
                AgentSession(
                    id=ident,
                    created_by_operator_id=1,
                    workspace_id=1,
                    tool=tool,
                    started_at=clock.now,
                )
            )
        session.commit()

        # Seed leases
        for ident in ("coordinator", "researcher_wal", "researcher_rollback"):
            session_liveness.renew_session_lease(
                session, session.get(AgentSession, ident), now=clock.now, create=True
            )
        session.commit()

    token = resolver.current_principal.set(principal)

    def forbidden(*args, **kwargs):
        raise AssertionError("peer coordination must not spawn or connect outward")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    yield SimpleNamespace(
        factory=factory, engine=engine, path=path, clock=clock, principal=principal
    )
    resolver.current_principal.reset(token)
    engine.dispose()


def test_adversarial_research_rounds_mode_with_interruption_and_dissent(panel_world) -> None:
    """Demonstrates adversarial research with independent blinding, rounds, crash recovery, and dissent (AC2, AC3)."""
    # 1. Coordinator proposes an adversarial architecture review
    spec = {
        "version": 1,
        "objective": "Determine default SQLite concurrency journal: WAL mode vs. Rollback journal.",
        "context": "High-concurrency multi-agent workspace reading and writing SQLite simultaneously.",
        "evidence_expectations": "Cite concrete benchmark, concurrency probe, or filesystem failure mode.",
        "discussion_rounds": 1,
        "exchange_mode": "rounds",
        "result_owner_session_id": "coordinator",
        "participants": [
            {"session_id": "researcher_wal", "model": "gpt-4o"},
            {"session_id": "researcher_rollback", "model": "claude-3-5-sonnet"},
        ],
    }

    proposal = peer.propose_coordination(
        PATH,
        "Adversarial Review: SQLite WAL vs Rollback",
        spec,
        session_id="coordinator",
        idempotency_key="prop-wal-rollback-1",
    )
    code = proposal["code"]
    assert proposal["status"] == "planned"
    assert proposal["specification"]["exchange_mode"] == "rounds"

    # Both specialist participants acknowledge and accept the exact specification
    acc_a = peer.accept_coordination(
        code,
        session_id="researcher_wal",
        version=proposal["version"],
        expected_revision=proposal["revision"],
        spec_hash=proposal["spec_hash"],
    )
    acc_b = peer.accept_coordination(
        code,
        session_id="researcher_rollback",
        version=acc_a["version"],
        expected_revision=acc_a["revision"],
        spec_hash=acc_a["spec_hash"],
    )
    assert acc_b["status"] == "accepted"

    # Coordinator advances to collecting initial independent research
    collecting = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=acc_b["version"],
        expected_revision=acc_b["revision"],
    )
    assert collecting["status"] == "collecting"
    assert collecting["blinded"] is True

    # 2. Phase 1: Independent First-Round Research (Blinded)
    # Researcher WAL submits findings supporting WAL with benchmark evidence
    wal_sub = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "WAL mode enables concurrent readers while writing, improving throughput 10x.",
            "evidence": "synthetic benchmark bench-wal-01: 1500 req/s vs 120 req/s",
            "uncertainty": "Untested on distributed/NFS file systems.",
            "dissent": "WAL mode checkpoint starvation occurs if long-lived readers hold read locks indefinitely.",
        },
        session_id="researcher_wal",
        version=collecting["version"],
        expected_revision=collecting["revision"],
        idempotency_key="sub-wal-init",
    )

    # CRUCIAL AC2 TEST: Researcher Rollback reads proposal before submitting.
    # Blinding MUST hide Researcher WAL's findings to prevent anchored/shared conclusions.
    rollback_view = peer.get_coordination(code, session_id="researcher_rollback")
    assert rollback_view["blinded"] is True
    # Rollback researcher sees only empty contributions (since own has not been submitted)
    assert len(rollback_view["contributions"]) == 0
    assert "bench-wal-01" not in str(rollback_view["contributions"])

    # --- SIMULATE COORDINATOR INTERRUPTION 1 (AC3) ---
    # Coordinator crashes/dies during initial collection phase.
    # Proves durable persistence in SQLite: state and unblinded submissions are safe.

    # Researcher Rollback submits independent findings opposing WAL
    rb_sub = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "Rollback journal provides strict single-file durability and zero risk of unbounded wal growth.",
            "evidence": "synthetic probe probe-fs-02: zero corruptions across power-loss simulation",
            "uncertainty": "Single writer locks all concurrent readers.",
            "dissent": "Rollback journal locks database entirely during writes, degrading multi-agent responsiveness.",
        },
        session_id="researcher_rollback",
        version=wal_sub["version"],
        expected_revision=wal_sub["revision"],
        idempotency_key="sub-rb-init",
    )

    # --- COORDINATOR RECOVERY 1 ---
    # Coordinator restarts, inspects state from database
    recovered_view = peer.get_coordination(code, session_id="coordinator")
    assert recovered_view["status"] == "collecting"
    assert recovered_view["counts"]["initial"] == 2

    # Coordinator advances coordination, which closes initial collection and UNBLINDS findings
    discussing = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=rb_sub["version"],
        expected_revision=rb_sub["revision"],
    )
    assert discussing["status"] == "discussing"
    assert discussing["initial_closed"] is True
    assert discussing["blinded"] is False
    assert discussing["round"] == 1

    # 3. Phase 2: Open Bounded Discussion & Rebuttal (Round 1)
    # Now both researchers can inspect each other's evidence
    wal_open_view = peer.get_coordination(code, session_id="researcher_wal")
    assert any("probe-fs-02" in c["payload"]["evidence"] for c in wal_open_view["contributions"])

    # Researcher WAL submits rebuttal in round 1 with targeted clarification
    wal_disc = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "Addressing Rollback: WAL file growth is mitigated by PRAGMA wal_autocheckpoint.",
            "evidence": "checkpoint monitor probe-cp-03",
            "uncertainty": "Requires operator to configure busy_timeout appropriately.",
            "dissent": "Rollback journal remains completely unacceptable for concurrent agents.",
            "clarifications": ["Did Rollback probe test concurrent reader timeouts?"],
        },
        session_id="researcher_wal",
        version=discussing["version"],
        expected_revision=discussing["revision"],
        idempotency_key="sub-wal-disc-1",
    )

    # Researcher Rollback submits rebuttal in round 1 with counter-evidence
    rb_disc = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "Addressing WAL: Network filesystems fail POSIX shared-memory locking needed by wal-index.",
            "evidence": "NFS lock test probe-nfs-04",
            "uncertainty": "Local SSD does not have NFS lock limitation.",
            "dissent": "WAL mode must not be default on unverified network shares.",
            "clarifications": [
                "Verified: concurrent readers timeout on rollback journal under 500ms bursts."
            ],
        },
        session_id="researcher_rollback",
        version=wal_disc["version"],
        expected_revision=wal_disc["revision"],
        idempotency_key="sub-rb-disc-1",
    )

    # --- SIMULATE COORDINATOR INTERRUPTION 2 (AC3) ---
    # Coordinator crashes midway through deliberation.

    # --- COORDINATOR RECOVERY 2 ---
    # Coordinator restarts, inspects progress
    recovered_disc = peer.get_coordination(code, session_id="coordinator")
    assert recovered_disc["status"] == "discussing"
    assert recovered_disc["counts"]["discussion"] == 2

    # Coordinator advances: closes discussion rounds, making proposal ready for final synthesis
    synthesis_ready = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=rb_disc["version"],
        expected_revision=rb_disc["revision"],
    )
    assert synthesis_ready["final_ready"] is True

    # 4. Phase 3: Final Synthesis Preserving Unresolved Dissent (AC2, AC3)
    final_result = peer.submit_coordination(
        code,
        "final",
        {
            "summary": "Consensus on WAL mode for local SSD storage; fallback to rollback journal for network storage.",
            "evidence": "Unified benchmark synthesis synth-wal-rb-01",
            "consensus": [
                "WAL mode provides required concurrency for local storage.",
                "Rollback journal is mandatory on network filesystem mounts.",
            ],
            "open_questions": [
                "Runtime detection mechanism for network mounts before enabling WAL.",
            ],
            "dissent": "Both specialists maintain conflicting edge-case dissents.",
        },
        session_id="coordinator",
        version=synthesis_ready["version"],
        expected_revision=synthesis_ready["revision"],
        idempotency_key="sub-final-synthesis",
    )

    assert final_result["status"] == "completed"
    assert final_result["incomplete_flag"] is False
    assert final_result["final"]["summary"].startswith("Consensus on WAL mode")
    assert len(final_result["final"]["consensus"]) == 2

    # CRITICAL AC2 & AC3 REQUIREMENT: Return dissent to requester rather than silently resolving away
    unresolved_dissent = final_result["unresolved_dissent"]
    assert len(unresolved_dissent) >= 2

    wal_dissents = [d for d in unresolved_dissent if d["author_session_id"] == "researcher_wal"]
    rb_dissents = [d for d in unresolved_dissent if d["author_session_id"] == "researcher_rollback"]

    assert len(wal_dissents) >= 1
    assert len(rb_dissents) >= 1
    assert all(d["resolved"] is False for d in unresolved_dissent)
    assert any(
        "Rollback journal remains completely unacceptable" in d["dissent"] for d in wal_dissents
    )
    assert any(
        "WAL mode must not be default on unverified network shares" in d["dissent"]
        for d in rb_dissents
    )


def test_adversarial_research_threaded_mode(panel_world) -> None:
    """Demonstrates threaded exchange mode with turn budgets, clarifications, and dissent preservation (AC2, AC3)."""
    spec = {
        "version": 1,
        "objective": "Determine optimal cache eviction policy: LRU vs. ARC.",
        "context": "In-memory query cache under skewed power-law access patterns.",
        "evidence_expectations": "Cite cache hit-rate traces and memory overhead.",
        "discussion_rounds": 1,
        "exchange_mode": "threaded",
        "max_turns_per_peer": 2,
        "result_owner_session_id": "coordinator",
        "participants": [
            {"session_id": "researcher_wal", "model": "model-a"},
            {"session_id": "researcher_rollback", "model": "model-b"},
        ],
    }

    proposal = peer.propose_coordination(
        PATH,
        "Adversarial Review: LRU vs ARC",
        spec,
        session_id="coordinator",
        idempotency_key="prop-lru-arc-1",
    )
    code = proposal["code"]

    # Acceptances
    acc_a = peer.accept_coordination(
        code,
        session_id="researcher_wal",
        version=proposal["version"],
        expected_revision=proposal["revision"],
        spec_hash=proposal["spec_hash"],
    )
    acc_b = peer.accept_coordination(
        code,
        session_id="researcher_rollback",
        version=acc_a["version"],
        expected_revision=acc_a["revision"],
        spec_hash=acc_a["spec_hash"],
    )

    # Advance to collecting
    col = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=acc_b["version"],
        expected_revision=acc_b["revision"],
    )
    assert col["status"] == "collecting"

    # Initial findings (blinded)
    sub_a = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "ARC adapts dynamically to recency and frequency, achieving 15% higher hit rate.",
            "evidence": "trace-arc-01: hit rate 82% vs 67%",
            "uncertainty": "Patent risk and higher memory metadata overhead.",
            "dissent": "ARC memory overhead is 2x that of simple LRU.",
        },
        session_id="researcher_wal",
        version=col["version"],
        expected_revision=col["revision"],
        idempotency_key="init-a",
    )
    sub_b = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "LRU has O(1) constant factor and minimal metadata, sufficient for predictable workloads.",
            "evidence": "trace-lru-01: 50ns lookup vs 180ns for ARC",
            "uncertainty": "Susceptible to cache pollution during scans.",
            "dissent": "LRU thrashes severely under large sequential scans.",
        },
        session_id="researcher_rollback",
        version=sub_a["version"],
        expected_revision=sub_a["revision"],
        idempotency_key="init-b",
    )

    # Advance to threaded discussion
    disc = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=sub_b["version"],
        expected_revision=sub_b["revision"],
    )
    assert disc["status"] == "discussing"
    assert disc["specification"]["exchange_mode"] == "threaded"

    # Threaded turns: Turn 1 for researcher_wal with clarification
    t1_a = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "ARC scan-resistance eliminates thrashing entirely.",
            "evidence": "scan probe scan-01",
            "uncertainty": "None for in-memory heap.",
            "dissent": "Objecting to LRU without 2Q or SLRU mitigation.",
            "clarifications": ["Does LRU implementation include scan resistance?"],
        },
        session_id="researcher_wal",
        version=disc["version"],
        expected_revision=disc["revision"],
        idempotency_key="turn-1-a",
    )

    # Threaded turns: Turn 1 for researcher_rollback answering clarification
    t1_b = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "Standard LRU does not, but 2Q is unpatented and gives ARC benefits.",
            "evidence": "2q evaluation 2q-probe-01",
            "uncertainty": "Requires two queues.",
            "dissent": "ARC patent licensing remains a legal blocker.",
            "clarifications": ["Clarified: proposing 2Q as unpatented alternative."],
        },
        session_id="researcher_rollback",
        version=t1_a["version"],
        expected_revision=t1_a["revision"],
        idempotency_key="turn-1-b",
    )

    # Advance to synthesis
    synth_ready = peer.advance_coordination(
        code,
        session_id="coordinator",
        version=t1_b["version"],
        expected_revision=t1_b["revision"],
    )
    assert synth_ready["final_ready"] is True

    # Final synthesis
    done = peer.submit_coordination(
        code,
        "final",
        {
            "summary": "Adopt 2Q cache policy combining scan-resistance with O(1) unencumbered implementation.",
            "evidence": "Unified cache evaluation report cache-eval-01",
            "consensus": [
                "Pure LRU is insufficient due to scan thrashing.",
                "2Q delivers ARC-grade hit rate.",
            ],
            "open_questions": ["Benchmark lock contention on 2Q hot paths."],
            "dissent": "Patent and memory dissents recorded from initial review.",
        },
        session_id="coordinator",
        version=synth_ready["version"],
        expected_revision=synth_ready["revision"],
        idempotency_key="final-synthesis",
    )
    assert done["status"] == "completed"
    assert len(done["unresolved_dissent"]) >= 2
    assert all(d["resolved"] is False for d in done["unresolved_dissent"])


def test_operator_orchestrates_adversarial_panel(panel_world) -> None:
    """Demonstrates human operator orchestrating an adversarial panel with unblinding control (AC2, AC5)."""
    spec = {
        "version": 1,
        "objective": "Operator review: evaluate TLS 1.3 0-RTT replay risk.",
        "context": "Edge proxy termination for authenticated API traffic.",
        "evidence_expectations": "Cite RFC 8446 security considerations.",
        "discussion_rounds": 1,
        "exchange_mode": "rounds",
        "result_owner_session_id": "coordinator",
        "participants": [
            {"session_id": "researcher_wal", "model": "sec-auditor-1"},
            {"session_id": "researcher_rollback", "model": "sec-auditor-2"},
        ],
    }

    # Operator proposes
    op_prop = peer.propose_operator_coordination(
        1,
        "TLS 1.3 0-RTT Security Review",
        spec,
        principal=panel_world.principal,
        idempotency_key="op-tls-review-1",
    )
    code = op_prop["code"]

    # Agents accept
    acc_a = peer.accept_coordination(
        code,
        session_id="researcher_wal",
        version=op_prop["version"],
        expected_revision=op_prop["revision"],
        spec_hash=op_prop["spec_hash"],
    )
    acc_b = peer.accept_coordination(
        code,
        session_id="researcher_rollback",
        version=acc_a["version"],
        expected_revision=acc_a["revision"],
        spec_hash=acc_a["spec_hash"],
    )
    acc_c = peer.accept_coordination(
        code,
        session_id="coordinator",
        version=acc_b["version"],
        expected_revision=acc_b["revision"],
        spec_hash=acc_b["spec_hash"],
    )

    # Operator advances to collecting
    op_col = peer.advance_operator_coordination(
        1,
        code,
        version=acc_c["version"],
        expected_revision=acc_c["revision"],
        principal=panel_world.principal,
    )
    assert op_col["status"] == "collecting"

    # Researcher 1 submits
    s1 = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "0-RTT allows replay of idempotent GET requests safely with single-use tickets.",
            "evidence": "RFC 8446 Section 8",
            "uncertainty": "Non-idempotent endpoints must reject early data.",
            "dissent": "Anti-replay cache is stateful and fails across cluster nodes.",
        },
        session_id="researcher_wal",
        version=op_col["version"],
        expected_revision=op_col["revision"],
        idempotency_key="tls-s1",
    )

    # Operator inspects: even operator snapshot hides initial bodies while collecting (blinding)
    op_view = peer.get_operator_coordination(1, code, principal=panel_world.principal)
    assert op_view["blinded"] is True
    assert len(op_view["contributions"]) == 0

    # Researcher 2 submits
    s2 = peer.submit_coordination(
        code,
        "initial",
        {
            "findings": "0-RTT must be disabled globally for all mutating APIs.",
            "evidence": "CWE-294 Replay Vulnerability in 0-RTT Early Data",
            "uncertainty": "Adds 1-RTT latency on reconnection.",
            "dissent": "Enabling 0-RTT creates catastrophic replay window for financial mutations.",
        },
        session_id="researcher_rollback",
        version=s1["version"],
        expected_revision=s1["revision"],
        idempotency_key="tls-s2",
    )

    # Operator advances: unblinds discussion
    op_disc = peer.advance_operator_coordination(
        1,
        code,
        version=s2["version"],
        expected_revision=s2["revision"],
        principal=panel_world.principal,
    )
    assert op_disc["status"] == "discussing"
    assert op_disc["initial_closed"] is True

    # Discussion contributions
    d1 = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "Agree to disable 0-RTT on all POST/PUT/DELETE; allow only on safe GET.",
            "evidence": "HTTP Method Idempotency RFC 9110",
            "uncertainty": "Requires gateway routing rule.",
            "dissent": "Still concerned about side-channel leaks on safe GET.",
            "clarifications": ["Can proxy reject Early-Data header on mutating paths?"],
        },
        session_id="researcher_wal",
        version=op_disc["version"],
        expected_revision=op_disc["revision"],
        idempotency_key="tls-d1",
    )
    d2 = peer.submit_coordination(
        code,
        "discussion",
        {
            "findings": "Gateway can return 425 Too Early on any mutating early data.",
            "evidence": "RFC 8470 Using Early Data in HTTP",
            "uncertainty": "Client must support retry on 425.",
            "dissent": "Must enforce 425 response header verification in CI tests.",
            "clarifications": ["Confirmed: 425 status code is supported by modern clients."],
        },
        session_id="researcher_rollback",
        version=d1["version"],
        expected_revision=d1["revision"],
        idempotency_key="tls-d2",
    )

    # Operator advances to synthesis
    op_synth_ready = peer.advance_operator_coordination(
        1,
        code,
        version=d2["version"],
        expected_revision=d2["revision"],
        principal=panel_world.principal,
    )
    assert op_synth_ready["final_ready"] is True

    # Result owner submits final synthesis
    done = peer.submit_coordination(
        code,
        "final",
        {
            "summary": "Allow 0-RTT only for safe GET endpoints; enforce 425 Too Early on all mutating requests.",
            "evidence": "RFC 8446 and RFC 8470 specification compliance matrix",
            "consensus": [
                "0-RTT forbidden on mutating methods.",
                "Gateway returns 425 Too Early on early data.",
            ],
            "open_questions": ["Automated regression test for 425 retry in test harness."],
            "dissent": "Specialist dissents on anti-replay clustering preserved.",
        },
        session_id="coordinator",
        version=op_synth_ready["version"],
        expected_revision=op_synth_ready["revision"],
        idempotency_key="tls-final",
    )
    assert done["status"] == "completed"
    assert len(done["unresolved_dissent"]) >= 2
    assert all(d["resolved"] is False for d in done["unresolved_dissent"])
