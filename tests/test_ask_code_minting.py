"""Regression tests for ASK/DEC approval-code minting.

``count() + 1`` was wrong in two ways that both matter for governance:

* a deleted row leaves a hole, so the next mint re-hands-out a code that is
  still in use (``approval_requests.code`` is unique, so the write dies - and
  an outward action that cannot file its ASK cannot be approved at all), and
* two writers filing at the same instant compute the same code, so one of them
  loses to the unique index.

An approval code is the identity a human approves and a governed action
spends, so both failures are governance failures. The fix is the same pattern
the coded-row tables already use: ``max(suffix) + 1``
(:func:`brains.control.common.next_sequential_code`) plus a retry on the
unique-index collision - here inside a savepoint, because the ASK is filed in
a transaction the caller owns.

Both paths that mint an ASK are covered: the decision API
(:func:`brains.control.decisions.file_decision_request`) and governed outward
authorization (:func:`brains.govern.authorize`, via ``_file_approval``).

The other half of the guarantee - that a code a governed action *spent* is
never re-minted after the ASK row it came from is pruned away - lives in
``test_governed_actions.py``, because it needs the full approve/deny/timeout
lifecycle to produce a permanently bound ``governed_actions.approval_code``.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from brains.control import decisions as decisions_mod
from brains.control.decisions import file_decision_request, get_decision, resolve_decision
from brains.control.sessions import register_workspace
from brains.govern import (
    STATUS_PENDING,
    TIER_OUTWARD,
    ActionTarget,
    GovernedRequest,
    authorize,
)
from brains.storage.db import SessionLocal
from brains.storage.migrations import init_db
from brains.storage.models import ApprovalDecision, ApprovalRequest


def _suffix(code: str) -> int:
    return int(code.split("-")[1])


def _live_codes(model) -> set[str]:
    init_db()
    with SessionLocal() as session:
        return {code for (code,) in session.query(model.code).all()}


def _delete_row(model, code: str) -> None:
    init_db()
    with SessionLocal() as session:
        row = session.query(model).filter(model.code == code).one()
        session.delete(row)
        session.commit()


def _force_first_collision(taken_code: str, prefix: str):
    """Make the next ``_next_code`` call for ``prefix`` return a taken code.

    Deterministically reproduces on SQLite the collision two simultaneous
    writers hit on a shared Postgres store.
    """
    real = decisions_mod._next_code
    state = {"calls": 0}

    def fake(session, table, code_prefix):
        if code_prefix == prefix:
            state["calls"] += 1
            if state["calls"] == 1:
                return taken_code
        return real(session, table, code_prefix)

    decisions_mod._next_code = fake
    return state, real


def _outward_request(tmp_path, name: str) -> GovernedRequest:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    registered = register_workspace(str(workspace))
    return GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="git",
        args=["push", "origin", name],
        target=ActionTarget(workspace_id=registered.id, workspace_path=str(workspace)),
        tier=TIER_OUTWARD,
        summary=f"git push origin {name}",
        cwd=str(workspace),
        idempotency_key=f"ask-mint:{uuid.uuid4().hex}",
    )


# ----------------------------------------------------------------------
# Deletion holes
# ----------------------------------------------------------------------


def test_ask_code_skips_a_deletion_hole(tmp_path):
    """A pruned approval must not make the next ASK re-use a live code."""
    workspace = str(tmp_path)
    first = file_decision_request(workspace, "first")["code"]
    middle = file_decision_request(workspace, "middle")["code"]
    last = file_decision_request(workspace, "last")["code"]
    assert _suffix(middle) == _suffix(first) + 1
    assert _suffix(last) == _suffix(middle) + 1

    _delete_row(ApprovalRequest, middle)

    minted = file_decision_request(workspace, "after the hole")["code"]

    assert minted not in {first, last}
    assert _suffix(minted) > _suffix(last), "a count()-based mint would re-issue a live code"
    assert get_decision(minted)["status"] == "open"


def test_governed_approval_code_skips_a_deletion_hole(tmp_path):
    """The same hole, reached through governed outward authorization."""
    workspace = str(tmp_path / "govern-hole")
    (tmp_path / "govern-hole").mkdir()
    doomed = file_decision_request(workspace, "will be pruned")["code"]
    file_decision_request(workspace, "still here")["code"]

    _delete_row(ApprovalRequest, doomed)

    decision = authorize(_outward_request(tmp_path, "ws-hole"), wait=False, notify=False)

    assert decision.status == STATUS_PENDING
    assert decision.approval_code is not None
    assert decision.approval_code != doomed
    assert get_decision(decision.approval_code)["status"] == "open"


def test_decision_code_skips_a_deletion_hole(tmp_path):
    """Decision codes are minted the same way, so they need the same guarantee."""
    workspace = str(tmp_path)
    asks = [file_decision_request(workspace, f"ask {index}")["code"] for index in range(3)]
    decisions = [resolve_decision(code, chosen="approve")["decision"] for code in asks]

    init_db()
    with SessionLocal() as session:
        ask = session.query(ApprovalRequest).filter(ApprovalRequest.code == asks[1]).one()
        ask.decision_id = None
        session.commit()
    _delete_row(ApprovalDecision, decisions[1])

    fresh = file_decision_request(workspace, "one more")["code"]
    minted = resolve_decision(fresh, chosen="approve")["decision"]

    assert minted not in _live_codes(ApprovalDecision) - {minted}
    assert _suffix(minted) > _suffix(decisions[2])


# ----------------------------------------------------------------------
# Concurrent minting
# ----------------------------------------------------------------------


def test_ask_filing_retries_a_code_collision(tmp_path):
    """A writer that loses the mint race retries instead of failing the caller."""
    workspace = str(tmp_path)
    taken = file_decision_request(workspace, "already filed")["code"]

    state, real = _force_first_collision(taken, "ASK")
    try:
        second = file_decision_request(workspace, "racing filing")
    finally:
        decisions_mod._next_code = real

    assert second["code"] != taken
    assert state["calls"] >= 2, "the collision was not retried"
    assert get_decision(second["code"])["status"] == "open"
    assert get_decision(taken)["title"] == "already filed"


def test_governed_authorization_retries_a_code_collision(tmp_path):
    """The gate must not be blocked by a lost mint race either.

    ``_file_approval`` files the ASK inside the transaction that also writes
    the audit entry, so the retry has to happen without discarding the
    caller's work.
    """
    workspace = str(tmp_path / "govern-race")
    (tmp_path / "govern-race").mkdir()
    taken = file_decision_request(workspace, "already filed")["code"]

    state, real = _force_first_collision(taken, "ASK")
    try:
        decision = authorize(_outward_request(tmp_path, "ws-race"), wait=False, notify=False)
    finally:
        decisions_mod._next_code = real

    assert decision.status == STATUS_PENDING
    assert decision.approval_code != taken
    assert state["calls"] >= 2
    assert get_decision(decision.approval_code)["status"] == "open"
    assert get_decision(taken)["title"] == "already filed"


def test_concurrent_ask_filings_mint_distinct_codes(tmp_path):
    """Four simultaneous filings produce four approvals, not one and three errors."""
    workspace = str(tmp_path)
    file_decision_request(workspace, "seed")
    barrier = threading.Barrier(4)
    minted: list[str] = []
    errors: list[BaseException] = []

    def _file(index: int) -> None:
        barrier.wait(timeout=30)
        try:
            minted.append(file_decision_request(workspace, f"concurrent {index}")["code"])
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_file, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(set(minted)) == 4, f"duplicate ASK codes minted: {minted}"
    for code in minted:
        assert get_decision(code)["status"] == "open"


def test_concurrent_governed_authorizations_mint_distinct_codes(tmp_path):
    """The same race, driven through the governed outward path."""
    requests = [_outward_request(tmp_path, f"ws-concurrent-{index}") for index in range(3)]
    barrier = threading.Barrier(len(requests))
    codes: list[str] = []
    errors: list[BaseException] = []

    def _authorize(request: GovernedRequest) -> None:
        barrier.wait(timeout=30)
        try:
            codes.append(authorize(request, wait=False, notify=False).approval_code)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_authorize, args=(request,)) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert None not in codes
    assert len(set(codes)) == len(requests), f"duplicate ASK codes minted: {codes}"


def test_ask_minting_gives_up_loudly_rather_than_writing_a_duplicate(tmp_path):
    """If every attempt collides, the write fails - it never lands a duplicate."""
    from sqlalchemy.exc import IntegrityError

    workspace = str(tmp_path)
    taken = file_decision_request(workspace, "permanent collision")["code"]
    real = decisions_mod._next_code
    decisions_mod._next_code = lambda session, table, prefix: (
        taken if prefix == "ASK" else real(session, table, prefix)
    )
    try:
        with pytest.raises(IntegrityError):
            file_decision_request(workspace, "doomed")
    finally:
        decisions_mod._next_code = real

    assert get_decision(taken)["title"] == "permanent collision"
