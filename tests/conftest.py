"""Pytest configuration shared by the brains test suite.

We pin ``BRAINS_API_KEY=local-dev-key`` *before* any brains module is
imported so the existing ``auth_headers`` fixture (and every test that
references ``settings.api_key`` directly) keeps working without per-test
environment plumbing. Production callers don't see this value — the
runtime default is empty and ``brains.api.admin_key.ensure_admin_key``
generates a real key on first run.

We *also* unconditionally redirect ``BRAINS_DB_URL`` and ``BRAINS_STATE_DIR``
to a process-local tmp dir so test runs cannot pollute the operator's real
``~/.brains/brains.db`` or inherit ambient state. The env MUST be set before
*any* brains import because ``brains.storage.db`` caches the SQLAlchemy engine
at module-load time; a later override has no effect on the same process.

Escape hatch: ``BRAINS_TEST_KEEP_AMBIENT_DB=1`` honours the caller's
ambient ``BRAINS_DB_URL`` / ``BRAINS_STATE_DIR`` (useful for running a
single test against a known fixture DB). Off by default.
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("BRAINS_API_KEY", "local-dev-key")
# Tests open hundreds of sessions; the on-by-default background pre-indexer would
# spawn a daemon graph-build/embed thread per session-start and race the test
# SQLite DB. Keep it OFF in tests (real default stays on). Tests that exercise
# prewarm enable it explicitly.
os.environ.setdefault("BRAINS_PREWARM_INDEX_ON_SESSION", "0")

if os.environ.get("BRAINS_TEST_KEEP_AMBIENT_DB") != "1":
    _test_state = Path(tempfile.mkdtemp(prefix="brains-test-state-"))
    os.environ["BRAINS_STATE_DIR"] = str(_test_state)
    # SQLAlchemy URL with a normalised forward-slash path; works on Windows too.
    _db_path = (_test_state / "brains.db").as_posix()
    os.environ["BRAINS_DB_URL"] = f"sqlite:///{_db_path}"

import pytest  # noqa: E402  (must follow env mutation above)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer local-dev-key"}
