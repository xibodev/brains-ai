"""The frozen baseline DDL and the generator that produces it.

Two properties are asserted here without needing a database:

1. The checked-in baseline is exactly what the generator renders for the tables
   it froze, and the generator is deterministic, so "regenerate the baseline"
   is a reproducible operation rather than a diff-producing one. The baseline
   is frozen, not current: tables added after the freeze are provisioned by
   numbered deltas, which this file also asserts.
2. Every guarded foreign key in the Postgres baseline is guarded on the
   *identity* of the constraint - constrained relation, constrained columns,
   referenced relation, referenced columns - and never on its name. A store
   provisioned by an older ``create_all`` carries the same foreign keys under
   Postgres' own ``<table>_<column>_fkey`` names, and a name-based guard would
   add a second, semantically identical constraint to every one of them.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from brains.storage.migration_registry import BASELINE_BLOCK_MARKER, baseline_path
from brains.storage.models import Base

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/generate_baseline_schema.py"
_SPEC = importlib.util.spec_from_file_location("generate_baseline_schema", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
generator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(generator)

_ADD_CONSTRAINT_RE = re.compile(
    r"ALTER TABLE (?P<table>\w+) ADD CONSTRAINT (?P<name>\w+) "
    r"FOREIGN KEY\((?P<columns>[^)]+)\) REFERENCES (?P<target>\w+) \((?P<target_columns>[^)]+)\)"
)


@pytest.fixture(autouse=True)
def _preserve_constraint_names():
    """Rendering names the deferred constraints; keep the shared metadata clean."""
    saved = [
        (constraint, constraint.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    ]
    yield
    for constraint, name in saved:
        constraint.name = name


def _always_blocks(ddl: str) -> list[str]:
    blocks = ddl.split(f"{BASELINE_BLOCK_MARKER} ")
    return [block for block in blocks[1:] if block.startswith("always")]


def _guard_predicate(block: str) -> str:
    """The text the guard decides on, i.e. everything before its ``THEN``."""
    head, separator, _ = block.partition(") THEN")
    assert separator, f"an always block without a guard: {block[:120]}"
    return head


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_render_is_deterministic(backend: str) -> None:
    assert generator.render(backend) == generator.render(backend)


def _blocks(ddl: str) -> set[tuple[str, str]]:
    """``{(block label, block body)}`` for one rendered or frozen baseline.

    Anchored at column zero so the header's own description of the marker is
    not mistaken for a block.
    """
    out: set[tuple[str, str]] = set()
    matches = list(re.finditer(rf"(?m)^{re.escape(BASELINE_BLOCK_MARKER)} (?P<label>.+)$", ddl))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(ddl)
        out.add((match.group("label").strip(), ddl[match.end() : end].rstrip("\n")))
    return out


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_every_frozen_baseline_block_still_renders_byte_identically(backend: str) -> None:
    """The frozen part of the schema stays byte-reproducible.

    The baseline is frozen, not current: its checksum is recorded in every
    ledger that ran it, so regenerating it is a hard refusal at startup and
    schema changes ship as numbered deltas instead. The reproducibility this
    asserts is therefore per *block*: everything the baseline provisions must
    still be exactly what the generator renders for it today, while tables
    added after the freeze are absent from the frozen file by design.
    """
    frozen = _blocks(baseline_path(backend).read_text(encoding="utf-8"))
    rendered = _blocks(generator.render(backend))

    assert frozen, f"no baseline blocks parsed for {backend}"
    missing = frozen - rendered
    assert not missing, f"{backend}: {len(missing)} baseline block(s) drifted: " + ", ".join(
        sorted(label for label, _ in missing)
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_model_tables_outside_the_baseline_are_provisioned_by_a_numbered_delta(
    backend: str,
) -> None:
    """Nothing may be model-declared and provisioned by nobody.

    A table added after the baseline froze has to be created by a numbered
    migration; otherwise a fresh install would pass this file's checks and
    then fail the runner's post-migration schema verification.
    """
    baseline_tables = {
        label.split("=", 1)[1]
        for label, _ in _blocks(baseline_path(backend).read_text(encoding="utf-8"))
        if label.startswith("table=")
    }
    declared = {table.name for table in Base.metadata.tables.values()} - {"schema_versions"}
    assert baseline_tables, f"no baseline tables parsed for {backend}"
    migrations = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/brains/storage/sql_migrations").iterdir()
        if path.suffix in {".py", ".sql"}
    )
    for table in sorted(declared - baseline_tables):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migrations, (
            f"{table} is model-declared, absent from the {backend} baseline, and not "
            "created by any numbered migration"
        )


def test_rendering_one_backend_does_not_disturb_the_other() -> None:
    """Rendering must not mutate the shared model metadata.

    ``AddConstraint`` marks its constraint as excluded from ``CREATE TABLE``,
    so a generator that rendered Postgres first used to emit a SQLite baseline
    with every inlined foreign key silently missing.
    """
    postgres = generator.render("postgresql")
    sqlite_first = generator.render("sqlite")

    assert generator.render("sqlite") == sqlite_first
    assert generator.render("postgresql") == postgres
    frozen = _blocks(baseline_path("sqlite").read_text(encoding="utf-8"))
    assert not frozen - _blocks(sqlite_first)
    assert "FOREIGN KEY(workspace_id) REFERENCES workspaces (id)" in generator.render("sqlite")


def test_postgres_baseline_never_guards_a_foreign_key_by_name() -> None:
    ddl = baseline_path("postgresql").read_text(encoding="utf-8")

    assert "conname" not in ddl, "a name-based guard duplicates create_all's constraints"

    blocks = _always_blocks(ddl)
    assert blocks, "the postgres baseline has no guarded constraints to check"
    for block in blocks:
        statement = _ADD_CONSTRAINT_RE.search(block)
        assert statement is not None, block[:160]
        predicate = _guard_predicate(block)
        assert "FROM pg_constraint c" in predicate
        assert "c.contype = 'f'" in predicate
        assert "c.conrelid = to_regclass" in predicate
        assert "c.confrelid = to_regclass" in predicate
        assert "c.conkey = ARRAY[" in predicate
        assert "c.confkey = ARRAY[" in predicate
        assert statement["name"] not in predicate, "the guard depends on the constraint name"


def test_every_postgres_guard_matches_its_own_statements_identity() -> None:
    ddl = baseline_path("postgresql").read_text(encoding="utf-8")
    blocks = _always_blocks(ddl)

    for block in blocks:
        statement = _ADD_CONSTRAINT_RE.search(block)
        assert statement is not None
        predicate = _guard_predicate(block)
        assert f"to_regclass('{statement['table']}')" in predicate
        assert f"to_regclass('{statement['target']}')" in predicate
        for column in (name.strip() for name in statement["columns"].split(",")):
            assert f"a.attrelid = c.conrelid AND a.attname = '{column}'" in predicate, (
                f"{statement['table']}.{column} is not part of its own guard"
            )
        for column in (name.strip() for name in statement["target_columns"].split(",")):
            assert f"a.attrelid = c.confrelid AND a.attname = '{column}'" in predicate

    add_constraints = ddl.count("ADD CONSTRAINT")
    assert add_constraints == len(blocks), "an ADD CONSTRAINT outside a guarded block"


def _synthetic_foreign_key(columns: int = 1):  # noqa: ANN202 - SQLAlchemy constraint
    from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, String, Table

    metadata = MetaData()
    if columns == 1:
        Table("parent", metadata, Column("id", Integer, primary_key=True))
        child = Table(
            "child",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer),
            ForeignKeyConstraint(["parent_id"], ["parent.id"], name="fk_child_parent_id_parent"),
        )
    else:
        Table(
            "parent",
            metadata,
            Column("tenant", String(16), primary_key=True),
            Column("id", Integer, primary_key=True),
        )
        child = Table(
            "child",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("tenant", String(16)),
            Column("parent_id", Integer),
            ForeignKeyConstraint(
                ["tenant", "parent_id"],
                ["parent.tenant", "parent.id"],
                name="fk_child_tenant_parent_id_parent",
            ),
        )
    return next(
        constraint
        for constraint in child.constraints
        if constraint.__class__.__name__ == "ForeignKeyConstraint"
    )


def test_guard_is_identical_however_the_constraint_is_named() -> None:
    """Name-independence, proven on a synthetic constraint the compiler builds."""
    constraint = _synthetic_foreign_key()

    guard = generator._foreign_key_guard(
        constraint,
        "ALTER TABLE child ADD CONSTRAINT fk_child_parent_id_parent "
        "FOREIGN KEY(parent_id) REFERENCES parent (id)",
    )
    renamed = generator._foreign_key_guard(
        constraint,
        "ALTER TABLE child ADD CONSTRAINT child_parent_id_fkey "
        "FOREIGN KEY(parent_id) REFERENCES parent (id)",
    )

    assert _guard_predicate(guard) == _guard_predicate(renamed)
    predicate = _guard_predicate(guard)
    assert "fk_child_parent_id_parent" not in predicate
    assert "child_parent_id_fkey" not in predicate
    assert "to_regclass('child')" in predicate
    assert "to_regclass('parent')" in predicate
    assert "a.attrelid = c.conrelid AND a.attname = 'parent_id'" in predicate
    assert "a.attrelid = c.confrelid AND a.attname = 'id'" in predicate


def test_guard_compares_composite_key_columns_in_order() -> None:
    constraint = _synthetic_foreign_key(columns=2)

    predicate = _guard_predicate(
        generator._foreign_key_guard(
            constraint,
            "ALTER TABLE child ADD CONSTRAINT fk_child_tenant_parent_id_parent "
            "FOREIGN KEY(tenant, parent_id) REFERENCES parent (tenant, id)",
        )
    )

    conkey = predicate.split("c.conkey = ARRAY[")[1].split("]::smallint[]")[0]
    confkey = predicate.split("c.confkey = ARRAY[")[1].split("]::smallint[]")[0]
    assert conkey.index("'tenant'") < conkey.index("'parent_id'")
    assert confkey.index("'tenant'") < confkey.index("'id'")
    assert conkey.count("SELECT a.attnum") == 2
    assert confkey.count("SELECT a.attnum") == 2


def test_baseline_files_are_shipped_as_package_data() -> None:
    """An installed wheel without the baseline cannot create a database."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"brains.storage" = ["baseline/*.sql", "sql_migrations/*.sql"]' in pyproject
    for backend in ("sqlite", "postgresql"):
        assert baseline_path(backend).is_file()
