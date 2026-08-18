"""Regression tests for concurrency-safe coded-row inserts.

The distributed battle test (two operators on two machines writing to one
shared Postgres) surfaced a real race: ``_next_code`` computed
``count() + 1`` with no guard, so two simultaneous writers minted the same
``TASK-000N`` / ``KNOW-000N`` code and the second insert died on the unique
index. The fix is :func:`brains.control.common.insert_with_code_retry`, which
catches the unique-constraint violation, rolls back, and retries with a
freshly-computed code, plus :func:`next_sequential_code`, which uses the max
numeric suffix (not a count) so archived/deleted rows can't cause re-mint.

These tests force the collision deterministically (on any backend, including
the SQLite test DB) by making ``_next_code`` return a taken code once.
"""

from __future__ import annotations

from brains.control import knowledge as knowledge_mod
from brains.control import tasks as tasks_mod
from brains.control.common import next_sequential_code
from brains.control.knowledge import add_knowledge_entry
from brains.control.tasks import create_task


def _force_first_collision(module, taken_code):
    """Patch ``module._next_code`` to return ``taken_code`` exactly once.

    Returns a counter dict so callers can assert a retry actually happened.
    """
    real = module._next_code
    state = {"calls": 0}

    def fake(session, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return taken_code
        return real(session, *args, **kwargs)

    module._next_code = fake
    return state, real


def test_create_task_retries_on_code_collision(tmp_path):
    workspace = str(tmp_path)
    first = create_task(workspace, title="first")

    state, real = _force_first_collision(tasks_mod, first["code"])
    try:
        second = create_task(workspace, title="second")
    finally:
        tasks_mod._next_code = real

    # The retry produced a fresh, distinct code rather than raising.
    assert second["code"] != first["code"]
    # _next_code was called at least twice: the forced collision + the retry.
    assert state["calls"] >= 2


def test_add_knowledge_entry_retries_on_code_collision(tmp_path):
    workspace = str(tmp_path)
    first = add_knowledge_entry(workspace, "caveat", "first caveat")

    state, real = _force_first_collision(knowledge_mod, first["code"])
    try:
        second = add_knowledge_entry(workspace, "caveat", "second caveat")
    finally:
        knowledge_mod._next_code = real

    assert second["code"] != first["code"]
    assert state["calls"] >= 2


def test_next_sequential_code_uses_max_suffix(tmp_path):
    """A gap from a deleted/archived row must not cause a code to be re-minted."""
    from brains.storage.db import SessionLocal
    from brains.storage.migrations import init_db
    from brains.storage.models import AgentTask

    workspace = str(tmp_path)
    a = create_task(workspace, title="one")["code"]
    b = create_task(workspace, title="two")["code"]
    c = create_task(workspace, title="three")["code"]
    nums = [int(code.split("-")[1]) for code in (a, b, c)]
    # Codes minted in this workspace burst are strictly consecutive.
    assert nums[1] == nums[0] + 1
    assert nums[2] == nums[1] + 1

    # Delete the middle row: a count()-based scheme would now re-hand-out the
    # latest code (collision). The max-suffix scheme must skip past the gap.
    init_db()
    with SessionLocal() as session:
        row = session.query(AgentTask).filter(AgentTask.code == b).one()
        session.delete(row)
        session.commit()
        nxt = next_sequential_code(session, AgentTask.code, "TASK")
    assert int(nxt.split("-")[1]) == nums[2] + 1
