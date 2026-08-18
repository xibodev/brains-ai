"""Emit the frozen baseline schema DDL for one backend.

The baseline is the first migration (``0000_baseline``): the DDL that creates
the whole product schema on a fresh database, checked in as an artifact so a
fresh install does not depend on the installed model code to decide what the
initial schema means.

Regenerating an *existing* baseline is a contract violation: its checksum is
recorded in every ledger that ran it, and the runner refuses a mismatch. This
script exists to produce a baseline once, and to produce a *new*, separately
numbered baseline if one is ever introduced deliberately.

The ledger table ``schema_versions`` is excluded on purpose: the migration
runner owns it and creates it before any migration runs, so there is exactly
one creator.

Usage::

    python scripts/generate_baseline_schema.py sqlite
    python scripts/generate_baseline_schema.py postgresql --out src/brains/storage/baseline
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import MetaData  # noqa: E402,F401  (re-exported for callers)
from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable  # noqa: E402
from sqlalchemy.sql.ddl import sort_tables_and_constraints  # noqa: E402

from brains.storage.migration_ledger import LEDGER_TABLE  # noqa: E402
from brains.storage.migration_registry import BASELINE_BLOCK_MARKER  # noqa: E402
from brains.storage.models import Base  # noqa: E402

_DIALECTS = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),
}

_HEADER = """-- Brains frozen baseline schema for the {backend} backend.
--
-- Migration ID: 0000_baseline
--
-- Generated once from the SQLAlchemy models by
-- scripts/generate_baseline_schema.py and then FROZEN. Every ledger that ran
-- this file records its checksum; editing it is a hard refusal at startup.
-- Schema changes are added as new numbered migrations under
-- src/brains/storage/sql_migrations/ instead.
--
-- The file is organised into blocks introduced by
--   -- @baseline-block: table=<name>
-- The migration runner executes a table block only when that table does not
-- exist yet, so the baseline provisions a table together with its indexes and
-- never touches a table an older store already has: those are brought forward
-- by the numbered deltas. Blocks marked `always` carry their own existence
-- guard: a foreign key is matched on its identity in pg_constraint - the
-- constrained relation, its constrained columns, the referenced relation and
-- its referenced columns - and never on its constraint name, so a store whose
-- foreign keys were created under Postgres' own <table>_<column>_fkey names
-- does not gain a second, semantically identical constraint.
--
-- The schema_versions ledger is intentionally absent: the migration runner
-- creates and upgrades it before any migration runs.
"""


def _constraint_name(constraint) -> str:  # noqa: ANN001 - SQLAlchemy constraint
    columns = "_".join(column.name for column in constraint.columns)
    target = list(constraint.elements)[0].column.table.name
    return f"fk_{constraint.table.name}_{columns}_{target}"[:63]


def _attnum_array(relation: str, columns: list[str]) -> str:
    """A ``pg_attribute``-resolved ``smallint[]`` of ``columns``, in order.

    ``pg_constraint.conkey``/``confkey`` hold attribute numbers, not names, so
    the guard resolves the declared column names to attnums and compares the
    arrays. Array equality is order- and length-sensitive, which is exactly the
    identity a foreign key's column list has.
    """
    entries = ",\n".join(
        "                  (SELECT a.attnum FROM pg_attribute a "
        f"WHERE a.attrelid = {relation} AND a.attname = '{column}')"
        for column in columns
    )
    return f"ARRAY[\n{entries}\n              ]::smallint[]"


def _foreign_key_guard(constraint, statement: str) -> str:  # noqa: ANN001 - SQLAlchemy constraint
    """An ``ADD CONSTRAINT`` guarded by the foreign key's *identity*, not its name.

    A store provisioned by an older ``create_all`` carries the same foreign keys
    under Postgres' own ``<table>_<column>_fkey`` names, so a guard that only
    looked for this generator's ``fk_...`` name would add a second,
    semantically identical constraint to every such table. The guard therefore
    matches on what makes two foreign keys the same edge: the constrained
    relation, its constrained columns, the referenced relation, and its
    referenced columns. A constraint that differs only in its referential
    action is still that same edge - adding a duplicate on top of it would
    enforce the same reference twice; changing an action is a numbered
    migration's job, not the baseline's.
    """
    local = [column.name for column in constraint.columns]
    elements = list(constraint.elements)
    target_table = elements[0].column.table.name
    target_columns = [element.column.name for element in elements]
    return (
        f"{BASELINE_BLOCK_MARKER} always\n"
        "DO $$\nBEGIN\n"
        "    IF NOT EXISTS (\n"
        "        SELECT 1\n"
        "        FROM pg_constraint c\n"
        "        WHERE c.contype = 'f'\n"
        f"          AND c.conrelid = to_regclass('{constraint.table.name}')\n"
        f"          AND c.confrelid = to_regclass('{target_table}')\n"
        f"          AND c.conkey = {_attnum_array('c.conrelid', local)}\n"
        f"          AND c.confkey = {_attnum_array('c.confrelid', target_columns)}\n"
        "    ) THEN\n"
        f"        {statement};\n"
        "    END IF;\n"
        "END\n$$;\n"
    )


def _indexes(table, dialect) -> list[str]:  # noqa: ANN001 - SQLAlchemy table
    return [
        str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n"
        for index in sorted(table.indexes, key=lambda item: item.name or "")
    ]


@contextlib.contextmanager
def _pristine_metadata():
    """Render without leaving a mark on the process-wide model metadata.

    Rendering mutates constraints: ``AddConstraint`` marks its constraint
    "already emitted, do not inline in CREATE TABLE", and a deferred constraint
    is given a deterministic name. Left in place, rendering Postgres first would
    silently drop every inlined foreign key from a SQLite baseline rendered
    afterwards in the same process.

    The state is restored rather than rendered from a copy on purpose:
    ``Table.to_metadata`` re-creates constraints in set-iteration order, which
    is not stable across processes, and a frozen baseline has to be
    byte-reproducible.
    """
    saved = [
        (constraint, constraint.name, getattr(constraint, "_create_rule", None))
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    ]
    try:
        yield
    finally:
        for constraint, name, create_rule in saved:
            constraint.name = name
            constraint._create_rule = create_rule


def render(backend: str) -> str:
    with _pristine_metadata():
        return _render(backend)


def _render(backend: str) -> str:
    dialect = _DIALECTS[backend]
    tables = [table for table in Base.metadata.tables.values() if table.name != LEDGER_TABLE]
    ordered = sort_tables_and_constraints(tables)

    parts: list[str] = [_HEADER.format(backend=backend)]
    deferred: list[str] = []

    for table, foreign_keys in ordered:
        if table is None:
            # Foreign keys that cannot be inlined without a forward reference.
            # SQLite tolerates forward references, so they stay inline there;
            # Postgres needs them as guarded post-hoc constraints.
            if backend == "sqlite":
                continue
            for constraint in sorted(foreign_keys, key=_constraint_name):
                constraint.name = _constraint_name(constraint)
                statement = str(AddConstraint(constraint).compile(dialect=dialect)).strip()
                deferred.append(_foreign_key_guard(constraint, statement))
            continue
        if backend == "sqlite":
            create = CreateTable(table, if_not_exists=True)
        else:
            create = CreateTable(
                table,
                include_foreign_key_constraints=foreign_keys,
                if_not_exists=True,
            )
        block = [f"{BASELINE_BLOCK_MARKER} table={table.name}"]
        block.append(str(create.compile(dialect=dialect)).strip() + ";\n")
        block.extend(_indexes(table, dialect))
        parts.append("\n".join(block))

    parts.extend(deferred)
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=sorted(_DIALECTS))
    parser.add_argument(
        "--out",
        default=None,
        help="Directory to write <backend>.sql into. Prints to stdout when omitted.",
    )
    args = parser.parse_args(argv)

    ddl = render(args.backend)
    if args.out is None:
        sys.stdout.write(ddl)
        return 0
    target = Path(args.out).resolve() / f"{args.backend}.sql"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ddl, encoding="utf-8", newline="\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
