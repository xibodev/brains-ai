"""Explicit Session/operator authorship without rewriting durable work history."""

from __future__ import annotations

import re
import sqlite3


def _rebuild(conn: sqlite3.Connection, table: str, constraint: str) -> None:
    columns = {row[1]: row for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if "creator_kind" in columns:
        return

    # Use the stored DDL, not today's ORM: retain every historical column,
    # primary/unique/check constraint and FK, including composite identities.
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()[0]
    body = ddl[ddl.index("(") :]
    # SQLite requires all column definitions before table constraints. Insert
    # beside the original author column, retaining its REFERENCES clause below.
    body, count = re.subn(
        r'([,(]\s*)(creator_session_id|"creator_session_id"|'
        r"`creator_session_id`|\[creator_session_id\])\s+"
        r"(VARCHAR\s*\(\s*32\s*\))\s+NOT\s+NULL\b",
        r"\1creator_kind VARCHAR(16) NOT NULL DEFAULT 'session', \2 \3",
        body,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise RuntimeError(f"unexpected {table} creator Session definition")
    end = body.rfind(")")
    body = (
        body[:end]
        + f", CONSTRAINT {constraint} CHECK ("
        + "(creator_kind = 'session' AND creator_session_id IS NOT NULL) OR "
        + "(creator_kind = 'operator' AND creator_session_id IS NULL))"
        + body[end:]
    )
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name",
        (table,),
    ).fetchall()
    temporary = f"_156_{table}"
    conn.execute(f'CREATE TABLE "{temporary}" {body}')
    names = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    conn.execute(f'INSERT INTO "{temporary}" ({names}) SELECT {names} FROM "{table}"')
    # 154/155 incoming FKs use NO ACTION, never CASCADE. Do not rename the old
    # parent: modern SQLite would rewrite the children's REFERENCES to it.
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for (sql,) in objects:
        conn.execute(sql)


def upgrade(conn: sqlite3.Connection) -> None:
    enforced = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    deferred = conn.execute("PRAGMA defer_foreign_keys").fetchone()[0]
    # The runner already owns BEGIN IMMEDIATE. A savepoint also makes a direct
    # replay atomic; neither commits nor rolls back the caller's transaction.
    conn.execute("SAVEPOINT operator_work_authorship")
    try:
        if enforced:
            conn.execute("PRAGMA defer_foreign_keys = ON")
        _rebuild(conn, "work_assignments", "ck_work_assignments_creator")
        _rebuild(conn, "coordination_proposals", "ck_coordination_creator")
        if enforced:
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("operator work authorship foreign key check failed")
            # DROP/recreate can leave deferred violation counters even though
            # the restored parent keys are valid. Reset only after a full FK
            # check. foreign_keys itself stays ON throughout, including inside
            # the active transaction (where toggling it would have no effect).
            conn.execute("PRAGMA defer_foreign_keys = OFF")
            if deferred:
                conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute("RELEASE SAVEPOINT operator_work_authorship")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT operator_work_authorship")
        conn.execute("RELEASE SAVEPOINT operator_work_authorship")
        raise
    finally:
        if enforced:
            conn.execute(f"PRAGMA defer_foreign_keys = {deferred}")
