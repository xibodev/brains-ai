"""Focused tests for the F1 Connect-a-machine enrolment layer.

Covers the control layer (mint/redeem happy-path + the three error paths) and
the 122 disk migration's idempotency. Mirrors ``tests/test_native_battalion.py``
fixture style: plain pytest functions on the conftest-isolated tmp DB.
"""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest

from brains.control import enrolment as enrol_ctl
from brains.storage.db import engine
from brains.storage.migrations import SQL_MIGRATIONS_DIR, init_db


@pytest.fixture(autouse=True)
def _bootstrap():
    init_db()
    yield


def _load_migration(filename: str):
    path = SQL_MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raw_sqlite_conn() -> sqlite3.Connection:
    raw = engine.raw_connection()
    conn = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
    assert isinstance(conn, sqlite3.Connection)
    return conn


# --------------------------------------------------------------------------- #
# Control — mint / redeem
# --------------------------------------------------------------------------- #


def test_mint_returns_raw_token_and_expiry():
    minted = enrol_ctl.mint_token(label="laptop", ttl_seconds=900)
    assert minted["token"]
    assert minted["expires_at"]
    assert minted["id"]
    assert minted["label"] == "laptop"


def test_mint_persists_only_hash_never_raw():
    minted = enrol_ctl.mint_token(label="laptop")
    raw = minted["token"]
    conn = _raw_sqlite_conn()
    rows = conn.execute(
        "SELECT token_hash FROM enrolment_tokens WHERE id = ?", (minted["id"],)
    ).fetchall()
    assert rows, "token row not persisted"
    for (stored,) in rows:
        assert stored != raw, "raw token must never be persisted"
        assert len(stored) == 64, "stored value should be a sha256 hex digest"


def test_redeem_happy_path_registers_one_runtime_per_cli():
    minted = enrol_ctl.mint_token(label="box")
    out = enrol_ctl.redeem_token(
        minted["token"],
        machine_id="box-1",
        clis=[
            {"tool": "copilot", "version": "1.0.65"},
            {"tool": "claude", "version": "2.0.1"},
        ],
    )
    assert out["machine_id"] == "box-1"
    runtimes = out["runtimes"]
    assert {r["tool"] for r in runtimes} == {"copilot", "claude"}
    assert all(r["version"] for r in runtimes), "runtimes must be version-stamped"
    # Version is also stamped into the persisted capabilities (parsed to a dict).
    by_tool = {r["tool"]: r for r in runtimes}
    caps = by_tool["copilot"]["capabilities"]
    assert caps["version"] == "1.0.65"


def test_redeem_without_clis_succeeds():
    minted = enrol_ctl.mint_token()
    out = enrol_ctl.redeem_token(minted["token"], machine_id="solo")
    assert out["machine_id"] == "solo"
    assert out["runtimes"] == []


# --------------------------------------------------------------------------- #
# Control — error paths
# --------------------------------------------------------------------------- #


def test_redeem_unknown_token_rejected():
    with pytest.raises(ValueError, match="invalid"):
        enrol_ctl.redeem_token("not-a-real-token", machine_id="m")


def test_redeem_single_use():
    minted = enrol_ctl.mint_token()
    enrol_ctl.redeem_token(minted["token"], machine_id="machine-A")
    with pytest.raises(ValueError, match="redeemed|invalid|used"):
        enrol_ctl.redeem_token(minted["token"], machine_id="machine-B")


def test_redeem_expired_token_rejected():
    expired = enrol_ctl.mint_token(ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired|invalid"):
        enrol_ctl.redeem_token(expired["token"], machine_id="machine-C")


# --------------------------------------------------------------------------- #
# Migration 122 — idempotency
# --------------------------------------------------------------------------- #


def test_migration_122_idempotent():
    init_db()
    mig = _load_migration("122_enrolment_tokens.py")
    conn = _raw_sqlite_conn()
    mig.upgrade(conn)
    conn.commit()
    mig.upgrade(conn)
    conn.commit()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(enrolment_tokens)")}
    assert {
        "id",
        "token_hash",
        "label",
        "org_id",
        "created_by_operator_id",
        "created_at",
        "expires_at",
        "redeemed_at",
        "redeemed_machine_id",
    } <= cols
