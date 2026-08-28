from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class SchemaVersion(Base):
    """The migration ledger: one row per migration ID.

    This table is infrastructure owned by :mod:`brains.storage.migrations`,
    not product schema. The runner creates and upgrades it itself before any
    migration runs, so it is deliberately excluded from the frozen baseline
    DDL under ``brains/storage/baseline``.

    A row is only ``status='applied'`` once that migration's backend-specific
    delta actually ran and committed. ``status='skipped'`` records a migration
    that has no implementation for this backend but whose target state the
    baseline already provisions; ``status='failed'`` and ``status='running'``
    record a delta that raised or was interrupted, with enough metadata
    (``error``, ``attempts``, ``started_at``) to diagnose it.

    ``checksum`` is the immutable content hash of the implementation that ran.
    ``checksum_origin`` distinguishes a hash the runner recorded when it ran
    the migration (``runner``) from one adopted for a row written by the
    pre-checksum ledger (``legacy-adopted``), which cannot be verified
    retroactively, and from one adopted for a legacy row that cannot be
    evidence of execution on this backend at all (``legacy-unproven``).

    ``applied_at`` is the timestamp of the last recorded outcome, whatever that
    outcome was; ``started_at``/``completed_at`` bound the attempt itself.
    """

    __tablename__ = "schema_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    migration_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checksum_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    runner_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Trace(Base):
    __tablename__ = "traces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RouteDecision(Base):
    __tablename__ = "route_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64))
    model_tier: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class FreshnessCheck(Base):
    __tablename__ = "freshness_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(512))
    check_type: Mapped[str] = mapped_column(String(32))
    metadata_json: Mapped[str] = mapped_column("metadata", Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    # Layer 2 of the multi-operator model (decision record 0002).
    # ``shared`` (default) means every operator can see this workspace
    # — back-compat with single-operator and unbounded multi-operator
    # installs. ``private`` means only operators with a row in
    # ``workspace_memberships`` (plus the auto-provisioned ``admin``
    # operator, who has implicit membership everywhere) can see it.
    visibility: Mapped[str] = mapped_column(String(16), default="shared")
    # WS2 (native-battalion): nullable Org back-reference. Existing installs
    # have org-less workspaces; the 120 migration seeds a default org and
    # backfills this, and the app treats NULL as "default org".
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    last_touched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Operator(Base):
    """A named human (or robot) principal that owns one or more sessions.

    The current identity model is documented in ``docs/ARCHITECTURE.md``.
    Each operator owns exactly one API key; the table stores only a truncated SHA-256
    fingerprint of that key so the raw secret never lives in the database.

    On first run, ``brains.control.operators.ensure_admin_operator`` auto-
    provisions a row with ``slug='admin'`` and the fingerprint of the
    auto-generated admin key — preserving the single-operator behaviour
    that every existing install relies on.
    """

    __tablename__ = "operators"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # First 16 hex chars of sha256(api_key). Same shape as the cookie
    # ``kid`` so the dashboard can map a signed cookie back to the
    # operator without re-reading the raw key from disk.
    key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class WorkspaceMembership(Base):
    """Per-workspace operator grant.

    The workspace trust boundary is documented in ``docs/ARCHITECTURE.md``.
    A row here means
    the given operator has explicit access to the workspace; it is the
    only way to see ``Workspace.visibility == 'private'`` rows. The
    auto-provisioned ``admin`` operator has implicit membership on every
    workspace and never needs a row.

    ``role`` is informational today (``member`` / ``owner``) — the
    visibility check is binary (member or not). The column is in place
    so a future layer can add per-row authorisation (e.g. only owners
    can resolve decisions) without another migration.
    """

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("operator_id", "workspace_id", name="uq_workspace_memberships_op_ws"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"), nullable=False, index=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (Index("ix_agent_sessions_ws_activity", "workspace_id", "last_activity_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Layer 1 of the multi-operator model: every session is stamped with
    # the operator who started it. Nullable so existing rows (pre-Layer-1)
    # remain valid and so test fixtures that bypass the resolver don't
    # break. New sessions written via ``control.sessions.start_session``
    # always carry a value (falls back to the auto-provisioned ``admin``
    # operator if nothing else matches).
    created_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # F3.2 lifecycle: spawning -> running -> dormant | blocked | completed | failed.
    # Defaults to ``running`` so pre-F3.2 rows (and create_all on fresh DBs) keep
    # the prior implied semantics; the 123 disk migration patches existing SQLite.
    state: Mapped[str] = mapped_column(String(16), default="running", index=True)
    # Updated opportunistically by ``append_event`` whenever a brain tool
    # call carries a ``session_id``. Lets the reaper and the resume UI
    # show how fresh a session actually is without forcing every agent
    # to call a dedicated heartbeat tool.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # WS2 (native-battalion): trace a live execution to the issue it runs,
    # the persona that ran it, and the runtime it ran on. All nullable +
    # indexed; the 121 migration patches existing SQLite DBs.
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True, index=True)
    persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True, index=True
    )
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtimes.id"), nullable=True, index=True
    )


class SessionSuccessor(Base):
    """Explicit predecessor -> successor handle continuity.

    Separate additive table: ``agent_sessions`` belongs to the frozen baseline
    and must not gain post-freeze columns in place.
    """

    __tablename__ = "session_successors"
    predecessor_session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id"), primary_key=True
    )
    successor_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class SessionLease(Base):
    """Renewable liveness for coordination Sessions without an owned PID."""

    __tablename__ = "session_leases"
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), primary_key=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_ws_created", "workspace_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    proposed_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    approval_request_id: Mapped[int] = mapped_column(ForeignKey("approval_requests.id"), index=True)
    chosen: Mapped[str] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Handoff(Base):
    __tablename__ = "handoffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    picked_up_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (Index("ix_agent_tasks_ws_status", "workspace_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="p2", index=True)
    status: Mapped[str] = mapped_column(String(32), default="available", index=True)
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    claimed_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    depends_on: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class WorkspaceClaim(Base):
    __tablename__ = "workspace_claims"
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    scope: Mapped[str] = mapped_column(String(64), default="code")
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class MailboxMessage(Base):
    __tablename__ = "mailbox_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    from_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="info", index=True)
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    data_json: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class KnowledgePattern(Base):
    __tablename__ = "knowledge_patterns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str] = mapped_column(Text)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    applies_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    proposed_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)


class RegisteredTool(Base):
    __tablename__ = "registered_tools"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    cli_command: Mapped[str] = mapped_column(String(512))
    spawn_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_available: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecurringTaskDefinition(Base):
    __tablename__ = "recurring_task_definitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title_template: Mapped[str] = mapped_column(String(256))
    body_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="p2")
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    cron_expr: Mapped[str] = mapped_column(String(64), default="manual")
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Auto-spawn columns added by sql_migrations/010_hivemind_consolidation.py.
    # Wired into recurring.fire_recurring_task() in Phase 2 PR-2; spawning
    # itself is gated behind BRAINS_ALLOW_RECURRING_SPAWN=1.
    spawn_tool: Mapped[str | None] = mapped_column(Text, nullable=True)
    spawn_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    spawn_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When set, the slug of a squad in this workspace. A fired recurring task is
    # tagged ``squad:<slug>`` so it routes to that squad's leader to delegate.
    # Added by sql_migrations/110_recurring_squad.py for existing DBs.
    squad: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    uri: Mapped[str] = mapped_column(String(1024), index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    path: Mapped[str] = mapped_column(String(1024), index=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class CodeGraphNode(Base):
    __tablename__ = "code_graph_nodes"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "path",
            "kind",
            "name",
            name="uq_code_graph_nodes_ws_path_kind_name",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # module | class | function | file
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024), index=True)
    lineno: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subsystem_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CodeGraphEdge(Base):
    __tablename__ = "code_graph_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    src_id: Mapped[int] = mapped_column(ForeignKey("code_graph_nodes.id"), index=True)
    dst_id: Mapped[int] = mapped_column(ForeignKey("code_graph_nodes.id"), index=True)
    # calls | imports | contains
    relation: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(16), default="extracted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ChunksMeta(Base):
    """Per-database embedding metadata (model + dim).

    Mirrors hivemind's ``chunks_meta``. A single row with ``id = 1`` records
    which embedding model was used to populate the ``chunks`` table; mixing
    models across a single database would corrupt similarity search, so we
    enforce one-row-per-DB at the application layer.
    """

    __tablename__ = "chunks_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    embed_model: Mapped[str] = mapped_column(String(128))
    embed_dim: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HelpRequest(Base):
    """Cross-session ask/answer RPC record.

    Backs the long-poll peer help protocol exposed by
    ``brains.control.help`` and the MCP tools ``ask_peer``,
    ``wait_for_request`` and ``answer_request``. Each row represents one
    question raised by ``from_session_id`` and (eventually) one answer
    produced by ``claimed_by_session_id``.

    Status transitions:

    * ``open``      — newly filed, no peer has claimed it yet.
    * ``claimed``   — a peer's ``wait_for_request`` returned this row;
                      ``claimed_by_session_id`` and ``claimed_at`` set.
    * ``answered``  — peer filed ``answer_request``; ``answer`` +
                      ``evidence`` + ``answered_at`` set.
    * ``expired``   — past ``expires_at`` with no answer. Asker sees the
                      timeout and decides whether to retry / fall back.
    * ``cancelled`` — asker withdrew (rare).

    ``to_workspace`` (slug) is the routing target. ``to_session_id`` is
    optional pinned routing. Either or both may be set; the matcher in
    ``wait_for_request`` honours both.
    """

    __tablename__ = "help_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    from_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    from_workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    to_workspace: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    to_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(256))
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    claimed_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ask_depth: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class HelpRequestConstraint(Base):
    """Optional harness constraint on a peer-help request (comms slice 1).

    A separate table rather than a column on the frozen ``help_requests``
    baseline: post-freeze schema changes ship as additive deltas, and the
    frozen table's rendering must stay byte-identical.

    Grammar of ``required_tool``: an exact tool name (``claude``) or
    ``not:<tool>`` (``not:copilot``), case-insensitive, matched against the
    claiming session's ``tool``. Absent row = any harness may claim.
    """

    __tablename__ = "help_request_constraints"
    request_code: Mapped[str] = mapped_column(ForeignKey("help_requests.code"), primary_key=True)
    required_tool: Mapped[str] = mapped_column(String(64))


class SecureSetting(Base):
    """Encrypted local configuration value.

    Values are AES-GCM ciphertext. ``nonce`` and ``salt`` are public random
    inputs; the encryption key is derived from the Brains admin key and never
    stored. Only a small allowlisted configuration surface may use this table.
    """

    __tablename__ = "secure_settings"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class TopicPost(Base):
    """One post on a named agent topic (message board, comms slice 1).

    Topics are install-wide and flat: any live session may post, replies
    reference their parent via ``reply_to_id``, and delivery to busy agents
    happens through the mailbox — posting blasts one notification per other
    workspace with live sessions (see ``brains.control.topics``), so an
    agent only ever polls its own inbox.
    """

    __tablename__ = "topic_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    from_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    from_workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("topic_posts.id"), nullable=True)
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Informational harness hint on the post (``claude`` / ``not:copilot``).
    #: Unlike help requests this is advisory in slice 1 — readers self-select.
    required_tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class ToolSessionLink(Base):
    """Many-to-one mapping from a tool's own session id to a brain session.

    Why: a single brain ``AgentSession`` is the durable coordination
    thread. The CLI tool driving it (Claude Code, Copilot CLI, Codex,
    custom wrappers) keeps its own session state on disk under its own
    id — and that id rotates whenever the tool restarts. When the
    operator restarts the tool and resumes a brain session, we need to
    record the new tool-side id without orphaning the old one. Hence
    one brain session ↔ many tool sessions.

    The composite ``(brain_session_id, tool, tool_session_id)`` is
    unique so re-running ``link_tool_session`` with the same triple is
    a no-op rather than an error — that matters because the linker is
    called on every ``start_session`` / ``resume_brain_session`` with
    auto-detected ids.

    ``linked_by`` is either ``"auto"`` (tool registered itself on start)
    or ``"operator"`` (operator pinned the link manually via the resume
    flow).
    """

    __tablename__ = "tool_session_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brain_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    tool: Mapped[str] = mapped_column(String(64), index=True)
    tool_session_id: Mapped[str] = mapped_column(String(256), index=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    linked_by: Mapped[str] = mapped_column(String(16), default="auto")


class SessionCheckpoint(Base):
    """Agent-authored cairn for resume after crash / compaction.

    Checkpoints are intentionally minimal — narrative + next-action +
    blockers + a pointer to the tool's local scratchpad. Brain does not
    try to mirror the tool's working memory; it stores the cairns so a
    resumed session can re-orient cheaply.

    Written by ``brains.checkpoint`` at natural breakpoints (end of a
    sub-task, before a long operation, before tool-side compaction).
    Surfaced on resume via ``resume_brain_session``'s ``last_checkpoint``
    field.
    """

    __tablename__ = "session_checkpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockers: Mapped[str | None] = mapped_column(Text, nullable=True)
    scratchpad_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class AuditLogEntry(Base):
    """Signed, hash-chained audit log entry.

    Each row stores an HMAC-SHA256 of ``prev_hash || canonical_json``
    using the operator-local secret (env ``BRAINS_AUDIT_KEY`` or the
    auto-generated ``~/.brains/audit-key`` file). The chain starts from
    the literal string ``"GENESIS"`` so the very first entry is still
    self-verifying. ``brains.audit.verify_chain`` recomputes every hash
    and reports the first divergence, which makes row-delete /
    row-mutate / row-insert tampering detectable as long as the audit
    key was not also compromised. See :mod:`brains.audit` for the full
    threat model.
    """

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    # Free-form actor identifier. Typical values: ``"session:<id>"`` for
    # an agent session, ``"admin:<op-slug>"`` for an operator action,
    # ``"system"`` for scheduler / background tasks.
    actor: Mapped[str] = mapped_column(String(128), index=True)
    # Dotted action name, e.g. ``provider.invoke``,
    # ``admin.overlay_write``, ``task.create``, ``task.handoff``.
    action: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True)
    # Canonical-JSON payload. The same canonicalisation must be used
    # when computing ``entry_hash`` or chain verification will fail.
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)


class UsageLedgerEntry(Base):
    """One row per successful gateway call, used to compute cost savings.

    The ``cost_actual_usd`` / ``cost_baseline_usd`` columns are
    nullable on purpose: the static price catalog
    (:mod:`brains.router.prices`) doesn't know every model out there,
    and we'd rather record the call with NULL costs than fabricate
    numbers. The dashboard's savings panel filters out NULL rows when
    computing headline totals and exposes them under a separate
    "unpriced calls" tile so the operator can see what they need to
    add to the price catalog overlay to get full coverage.

    ``savings_usd`` is the convenience denormalised
    ``cost_baseline_usd - cost_actual_usd``. Stored at write time
    instead of computed at read time so the dashboard query is a
    plain ``SUM(savings_usd)`` over a time window.
    """

    __tablename__ = "usage_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    # Which gateway endpoint produced this row. Used so dashboards can
    # split "Claude clients" vs "OpenAI clients" if the operator cares.
    endpoint: Mapped[str] = mapped_column(String(64), index=True)
    # What the client asked for (``req.model``). Empty string allowed.
    requested_model: Mapped[str] = mapped_column(String(128), default="")
    # What brains actually called. Often differs from ``requested_model``
    # when the router is on; equals it when the router is off.
    routed_model: Mapped[str] = mapped_column(String(128), index=True)
    # Short provider id (``"openai"``, ``"anthropic"``, ``"ollama"``...).
    provider: Mapped[str] = mapped_column(String(64), index=True)
    # Optional classifier label (``"trivial"``, ``"code_fix"``, ...).
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # USD cost of the actual routed call. NULL when the price catalog
    # has no entry for ``routed_model``.
    cost_actual_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # USD cost the same call would have incurred against the configured
    # ``savings.baseline_model``. NULL when the catalog has no entry
    # for the baseline.
    cost_baseline_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Pre-computed ``cost_baseline_usd - cost_actual_usd``. Positive =
    # savings, negative = the router picked a more expensive model
    # than the baseline (rare, possible with mis-tiered configs).
    # NULL when either side of the subtraction was NULL.
    savings_usd: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # ``True`` when the routed provider is a built-in stub (e.g. the
    # ``echo`` dev provider). Aggregators exclude stub rows from the
    # dashboard headline by default so synthetic dev traffic doesn't
    # inflate the savings number; the rows are still kept for audit.
    is_stub: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0", index=True
    )
    # Deterministic A/B holdout marker for counterfactual savings rigor.
    # Default OFF; record_usage flips a stable fraction after the row gets its id.
    is_holdout: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0", index=True
    )


class KnowledgeEntry(Base):
    """Cross-operator knowledge ledger entry.

    A durable, visibility-scoped record of a blocker / workaround / resolution /
    caveat / environment- or dependency-note, with a lifecycle (proposed ->
    active -> confirmed -> resolved -> superseded / rejected / stale) and a
    supersede chain. Unlike the deliberately global ``knowledge_patterns``
    library, ledger entries are workspace-scoped and visibility-filtered by the
    current product contract; ``scope`` widens reach beyond a single workspace
    for genuinely shared knowledge. See ``docs/product/FEATURE_CONTRACT.md``.
    """

    __tablename__ = "knowledge_entries"
    __table_args__ = (Index("ix_knowledge_ws_status", "workspace_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # blocker | workaround | resolution | caveat | environment_note | dependency_note
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    # proposed | active | confirmed | resolved | superseded | rejected | stale
    status: Mapped[str] = mapped_column(String(16), index=True, default="active")
    # private | workspace | shared | global  (neutral scope taxonomy)
    scope: Mapped[str] = mapped_column(String(16), index=True, default="workspace")
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    tags: Mapped[str] = mapped_column(String(300), default="")
    confidence: Mapped[str] = mapped_column(String(16), default="medium")
    # How the entry was derived: extracted | inferred | ambiguous.
    provenance: Mapped[str] = mapped_column(String(16), default="inferred")
    importance: Mapped[float] = mapped_column(Float, default=0.5, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    promoted_from: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_entries.id"), nullable=True
    )
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_entries.id"), nullable=True
    )
    evidence: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Squad(Base):
    """A named group of operators with a designated leader, scoped to a workspace.

    A *squad* is a stable assignee: work routed to the squad is handed to its
    **leader** operator, who decides which member should pick it up and
    delegates via a mailbox message. This layers team-style routing on top of
    the existing pull-based task model — assigners address ``@squad`` instead
    of guessing which individual operator is free, and routing stays stable as
    membership changes.

    The leader is an operator (typically driven by an AI agent) — the routing
    *decision* is made by that agent from a roster brief, not by a hardcoded
    algorithm in brains.
    """

    __tablename__ = "squads"
    __table_args__ = (UniqueConstraint("workspace_id", "slug", name="uq_squad_workspace_slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    leader_operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SquadMember(Base):
    """An operator's membership in a squad, with an informational role.

    The leader is also stored here (role ``leader``) so the roster is a single
    source of truth. ``role`` is free text (e.g. ``leader`` / ``member`` /
    ``frontend``) and surfaces in the leader's routing brief alongside each
    member's skills (the operator's patterns/knowledge).
    """

    __tablename__ = "squad_members"
    __table_args__ = (UniqueConstraint("squad_id", "operator_id", name="uq_squad_member"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    squad_id: Mapped[int] = mapped_column(ForeignKey("squads.id"), index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"), index=True)
    role: Mapped[str] = mapped_column(String(64), default="member")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RecurringRun(Base):
    """An audit record of one recurring-task fire (the autopilot "run").

    Every fire — whether triggered by the cron scheduler, a manual invocation,
    or a webhook — writes a row here, giving recurring tasks a per-run audit
    trail and state machine instead of only a ``last_fired_at`` timestamp.

    ``source`` is ``schedule`` | ``manual`` | ``webhook``. ``status`` is
    ``created`` (task minted) | ``completed`` | ``failed`` | ``skipped`` (e.g.
    the definition was disabled). ``trigger_payload`` carries the normalized
    webhook body (if any) for debugging; secrets are never stored here.
    """

    __tablename__ = "recurring_runs"
    __table_args__ = (Index("ix_recurring_runs_def", "definition_name", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_name: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)
    task_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trigger_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class WebhookTrigger(Base):
    """An inbound webhook that fires a recurring-task definition.

    External systems POST to ``/hooks/<slug>`` with an ``Authorization: Bearer``
    token. Only a salted hash of the token is stored (never the plaintext);
    the plaintext is returned exactly once at creation time. An optional
    ``event_filter`` (a ``key=value`` against the JSON body) gates whether the
    delivery fires, so one endpoint can ignore irrelevant events.
    """

    __tablename__ = "webhook_triggers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    definition_name: Mapped[str] = mapped_column(String(128), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    event_filter: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class WebhookDelivery(Base):
    """A record of one inbound webhook delivery, used for idempotency.

    The ``(trigger_id, dedupe_key)`` pair is unique: a redelivery carrying the
    same dedupe key is acknowledged but does not fire the task twice.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("trigger_id", "dedupe_key", name="uq_webhook_delivery"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger_id: Mapped[int] = mapped_column(ForeignKey("webhook_triggers.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="fired")
    task_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class IntegrationDelivery(Base):
    """Durable inbound/outbound integration result and dedupe record."""

    __tablename__ = "integration_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "direction",
            "delivery_key",
            name="uq_integration_delivery",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    delivery_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), index=True)
    subject: Mapped[str | None] = mapped_column(String(256), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# WS2 — native-battalion: Org / Persona / Runtime / Project / Issue layer.
#
# Additive only. New tables are declared on ``Base`` so
# ``Base.metadata.create_all`` provisions them on fresh SQLite + Postgres DBs;
# the numbered disk migrations (120/121) patch the new *columns* on existing
# SQLite installs. See ``docs/ARCHITECTURE.md`` for the current data model.
# ---------------------------------------------------------------------------


class Org(Base):
    """Top-level container — an organisation that owns workspaces, personas,
    runtimes, projects and issues.

    Back-compat: existing installs are org-less. The 120 migration seeds one
    ``slug='default'`` org and backfills ``workspaces.org_id`` to it; the app
    treats a NULL ``org_id`` as "the default org".
    """

    __tablename__ = "orgs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class OrgMember(Base):
    """Org ↔ Operator membership. Mirrors ``workspace_memberships``."""

    __tablename__ = "org_members"
    __table_args__ = (UniqueConstraint("org_id", "operator_id", name="uq_org_member"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("operators.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Runtime(Base):
    """A registered CLI on a daemon-managed machine (tool × machine).

    The WS1 daemon registers + heartbeats these. Liveness is derived from
    ``status`` / ``health`` / ``last_heartbeat_at``; a sweeper flips stale
    rows to ``offline``.
    """

    __tablename__ = "runtimes"
    __table_args__ = (UniqueConstraint("machine_id", "tool", name="uq_runtime_machine_tool"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    machine_id: Mapped[str] = mapped_column(String(64), index=True)
    machine_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool: Mapped[str] = mapped_column(ForeignKey("registered_tools.name"), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    daemon_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os: Mapped[str | None] = mapped_column(String(32), nullable=True)
    working_root: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="offline", index=True)
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EnrolmentToken(Base):
    """A single-use, expiring connect token for the Connect-a-machine flow (F1).

    The token is the credential a new machine presents to register its CLIs
    without an operator API key. We persist ONLY the sha256 hash of the raw
    token (never the raw value); the raw token is returned to the operator once
    at mint time and embedded in the connect command.
    """

    __tablename__ = "enrolment_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    created_by_operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_machine_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApiCredential(Base):
    """One accepted HTTP credential, bound to exactly one principal (BL-P0-01).

    Every API key or console cookie that authenticates against Brains resolves
    to a row here, so authentication yields *one explicit principal* instead of
    membership in a broad key set. The raw secret is never stored: the row
    keeps only ``secret_hash`` (sha256 hex of the raw secret), which is also
    the lookup key, so verification is a hash lookup rather than a comparison
    against every accepted key.

    ``kind`` says what the credential is allowed to be:

    ``admin``
        The bootstrap admin key (``settings.api_key``) and its rotation
        siblings (``settings.api_keys``). Bound to the ``admin`` operator.
    ``operator``
        A per-operator key minted by ``brains.control.operators.add_operator``
        and stored under ``~/.brains/operator-keys/<slug>.key``.
    ``runtime``
        A Runtime-narrow credential minted by enrolment redemption. It is
        bound to one Org and one machine and authorizes only the Runtime
        operations of that machine - never an operator or admin API.

    ``revoked_at`` and ``expires_at`` are both honoured on every resolution, so
    revocation and expiry take effect without restarting the process.
    """

    __tablename__ = "api_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Public, non-secret handle an operator can use to revoke this credential.
    credential_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: sha256 hex of the raw secret. The raw secret is never persisted.
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: Truncated fingerprint, matching ``operators.key_fingerprint`` shape, for
    #: display and for correlating a legacy operator row with its credential.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtimes.id"), nullable=True, index=True
    )
    machine_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Where this credential came from, so reconciliation can own exactly the
    #: rows it created: ``local:admin_key`` / ``local:api_keys`` /
    #: ``local:operator_key`` are adopted from disk and are revoked when their
    #: raw value disappears from that source (a rotation, a deleted key file);
    #: ``enrolment`` and ``manual`` are never touched by reconciliation.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_by_operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Persona(Base):
    """A named AI identity = identity + brain (``model``) + hands (``tool`` +
    ``default_runtime_id``) + principal (``operator_id``).

    Binding ``operator_id`` lets a persona reuse the whole existing principal
    machinery (sessions, claims, squad membership, knowledge authorship). It is
    nullable now and bound 1:1 at first spawn.
    """

    __tablename__ = "personas"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_persona_org_slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool: Mapped[str | None] = mapped_column(ForeignKey("registered_tools.name"), nullable=True)
    default_runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtimes.id"), nullable=True, index=True
    )
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Project(Base):
    """A work container scoped to an org, with a primary repo (``workspace_id``)
    and an optional owning Pod (``assignee_pod_id`` → ``squads.id``)."""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_project_org_slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    assignee_pod_id: Mapped[int | None] = mapped_column(
        ForeignKey("squads.id"), nullable=True, index=True
    )
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Issue(Base):
    """A unit of work on the issue board. Assignment is tri-modal (persona /
    pod / operator), not mutually exclusive at the schema level — the app
    enforces precedence. ``closed_at`` is stamped on terminal states."""

    __tablename__ = "issues"
    __table_args__ = (Index("ix_issues_project_status", "project_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    parent_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="p2", index=True)
    assignee_persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True, index=True
    )
    assignee_pod_id: Mapped[int | None] = mapped_column(
        ForeignKey("squads.id"), nullable=True, index=True
    )
    assignee_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    agent_task_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    labels: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IssueComment(Base):
    """A comment on an issue (F3.3). Authored by a human operator, a persona, or
    a running session — so an agent can post a reasoned update (e.g. why it
    blocked) that surfaces on the issue alongside human comments."""

    __tablename__ = "issue_comments"
    __table_args__ = (Index("ix_issue_comments_issue_created", "issue_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text)
    author_kind: Mapped[str] = mapped_column(
        String(16), default="operator"
    )  # operator|persona|system
    author_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True
    )
    author_persona_id: Mapped[int | None] = mapped_column(ForeignKey("personas.id"), nullable=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class Skill(Base):
    """A named SKILL.md context pack (F10), org-scoped, attachable to personas/
    projects to compose agent context."""

    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_org_slug", "org_id", "slug"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class PersonaSkill(Base):
    """A Skill attached to a Persona (BL-P1-08/F10).

    ``(persona_id, skill_id)`` is unique, so attaching the same Skill twice
    updates nothing rather than duplicating context. ``position`` orders
    multiple attachments deterministically when they are assembled into a
    spawned Session's context; ``attached_by_operator_id``/``attached_at`` are
    provenance for who attached it and when.
    """

    __tablename__ = "persona_skills"
    __table_args__ = (UniqueConstraint("persona_id", "skill_id", name="uq_persona_skill"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attached_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True
    )
    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ProjectSkill(Base):
    """A Skill attached to a Project (BL-P1-08/F10). Same shape as
    :class:`PersonaSkill`, scoped to a Project instead of a Persona."""

    __tablename__ = "project_skills"
    __table_args__ = (UniqueConstraint("project_id", "skill_id", name="uq_project_skill"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attached_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True
    )
    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AuditChainHead(Base):
    """The single-row head pointer that serialises audit appends (B4).

    ``brains.audit`` used to compute ``prev_hash`` from ``MAX(id)`` under a
    process-local :class:`threading.Lock`, so two Brains processes writing the
    same store could both read the same predecessor and fork the chain. The
    head row makes the append a *write* that every appender must take first:
    SQLite escalates the transaction to a reserved lock, Postgres takes the
    row lock via ``SELECT ... FOR UPDATE``. Whoever loses waits or retries; no
    two appends can read the same predecessor.

    It also gives verification an O(1) anchor that survives truncation:
    ``seq`` counts every entry ever appended and ``head_entry_id`` /
    ``head_hash`` name the newest one, so deleting the tail of ``audit_log``
    is detectable even though the remaining rows still chain cleanly.
    """

    __tablename__ = "audit_chain_head"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), default="GENESIS")
    head_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # HMAC over ``seq|head_hash|head_entry_id`` with the audit key. Without it
    # an attacker who can write the database could truncate ``audit_log`` and
    # move the head to match, leaving a shorter chain that still verifies.
    # NULL is legal in exactly one state: a genuine pre-signature store that
    # has not been adopted yet. Over a non-empty log it fails verification and
    # refuses every append, so clearing the signature cannot launder a
    # truncation as "legacy".
    head_mac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The store's persisted commitment to signed heads. Once set, a missing
    # ``head_mac`` is tamper rather than legacy state, and ``adopt_legacy_chain``
    # refuses to sign anything: adoption happens once, explicitly, and only
    # after the chain, the head triple and the append count all verify.
    adopted_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class GovernedAction(Base):
    """One governed action: the durable spine of :mod:`brains.govern` (B4, F10).

    Every path that can produce an outward effect - PATH-shim gate, in-process
    subprocess spawn, recurring/autopilot fire, CLI/MCP outward tools - files
    one row here before anything runs, and the row advances through
    ``requested -> pending -> authorized -> executing -> succeeded|failed`` or
    terminates as ``denied``/``expired``. The row is written in the same
    transaction as the audit entry that records the same transition, so a
    store can never contain an authorised action with no audit record.

    Two uniqueness rules carry the guarantees the approval contract needs:

    ``idempotency_key``
        A retry of the same logical action reuses the row instead of creating
        a second one, so a repeated call cannot execute the effect twice or
        file a second approval decision.

    ``approval_code``
        An approval can be consumed by exactly one governed action. The
        conditional claim plus this constraint is what makes consumption
        atomic across processes rather than a read-then-write race.

    ``args_hash`` is a SHA-256 over the normalised argument vector, never the
    arguments themselves, so an approval is bound to the exact command that
    was reviewed without persisting secrets that appear in argv.
    """

    __tablename__ = "governed_actions"
    __table_args__ = (Index("ix_governed_actions_status_created", "status", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(128))
    args_hash: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    approval_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    issue_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # When the *current* attempt started. The lease that decides whether an
    # in-flight row was abandoned is per attempt, not per row: after a retry
    # resets an abandoned attempt, ``created_at`` is still old, so a lease
    # keyed on it would declare the fresh attempt abandoned immediately and
    # let concurrent retries each start their own.
    attempt_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Proof of life for an attempt that is *executing*. A long command holds
    # the row past any fixed attempt lease, so the sweep would eventually
    # record a running execution as abandoned. The owner renews this column
    # while the effect runs, and only the sweep's silence budget
    # (``BRAINS_EXECUTION_LEASE_SECONDS``) - not the total runtime - decides
    # whether an executing row is still alive.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_result_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RealtimeEvent(Base):
    """One durable realtime event (BL-P0-02).

    The realtime bus is a *notification* mechanism, not the record. Every
    event whose loss a user would notice - Session lifecycle, Issue change,
    approval/ASK movement, Runtime state - is committed here first and only
    then announced, so a client that was disconnected, or connected to a
    different process, can catch up by cursor instead of by luck.

    ``id`` is the monotonic cursor clients hold: it is assigned by the store,
    never by a producer, so ordering survives a process restart. ``dedupe_key``
    makes a re-published event idempotent - a retry writes no second row and
    therefore delivers no second envelope - and ``org_id``/``workspace_id``
    carry the scope the topic resolved to, so delivery can be filtered on the
    event's own scope rather than trusting the topic string alone.

    Rows are pruned by count (``BRAINS_REALTIME_RETENTION_ROWS``); a cursor
    older than the oldest retained row is answered with an explicit reset
    rather than with a silently short replay.
    """

    __tablename__ = "realtime_events"
    __table_args__ = (Index("ix_realtime_events_topic_id", "topic", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(160), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class SessionCommand(Base):
    """One durable operator command addressed to a Session (BL-P0-05).

    A message typed into the console and a stop pressed on a Session are
    *requests*, not effects: the operator's browser is not the process that
    can deliver them, and the Runtime that can may be busy, restarting or
    gone. Recording the request is therefore the first thing that happens,
    before anything is announced and long before anything is delivered, so a
    reload shows what was asked for and what became of it rather than an
    optimistic bubble that no longer exists.

    Four rules carry the guarantees the contract needs:

    ``operation_key``
        Unique. A retry of the same logical command - the same browser submit
        replayed, a stop pressed twice, a request re-sent after a timeout -
        reuses the existing row instead of queueing a second delivery, so a
        retry has exactly one logical outcome.

    ``(session_id, sequence)``
        Unique and dense per Session. Commands are delivered in the order they
        were accepted, and the pair is the stable ordering key a consumer and
        the console both read.

    ``claimed_by`` + ``lease_expires_at``
        A claim is a conditional update from ``requested`` to ``delivered``
        that stamps the consumer and a lease. Two Runtimes racing one command
        resolve to exactly one winner, and a consumer that dies mid-flight
        does not strand the command: the lease expires, the row returns to
        ``requested`` with ``attempt`` incremented, and another consumer -
        or the same one after a restart - picks it up.

    ``status``
        ``requested -> delivered -> acknowledged`` for a command that reached
        the agent, ``failed`` for one that truthfully could not (an agent
        process that accepts no interactive input is a failure, not a
        delivery), and ``cancelled`` for one the Session outlived.
    """

    __tablename__ = "session_commands"
    __table_args__ = (
        Index("ux_session_commands_session_sequence", "session_id", "sequence", unique=True),
        Index("ix_session_commands_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    #: The idempotency key. Derived from the caller's operation id where one is
    #: supplied, and from the Session for a stop, which is naturally one
    #: logical operation per Session.
    operation_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtimes.id"), nullable=True, index=True
    )
    machine_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PodProfile(Base):
    """The Persona-oriented product record for a Pod (BL-P1-03).

    A Pod is a team of **Personas**, not of operator labels. The legacy
    ``squads`` row remains the Pod's identity - ``issues.assignee_pod_id`` and
    ``projects.assignee_pod_id`` reference it, and the legacy workspace task
    routing still uses its operator columns - so the product record is stored
    beside it rather than by mutating a frozen baseline table.

    ``org_id`` is the Pod's Org. It is nullable only for legacy rows whose
    Workspace carried no Org; every Pod created through the Pod API has one,
    and membership is refused across Orgs.

    ``leader_persona_id`` is the one leader. The legacy
    ``squads.leader_operator_id`` records the operator principal that owns the
    legacy row and is no longer the product authority.
    """

    __tablename__ = "pod_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pod_id: Mapped[int] = mapped_column(ForeignKey("squads.id"), unique=True, index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    leader_persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PodMember(Base):
    """One Persona's membership in a Pod (BL-P1-03).

    ``(pod_id, persona_id)`` is unique, so adding the same Persona twice
    updates its role instead of duplicating the roster. ``source`` records
    where the row came from: ``api`` for a membership an operator created and
    ``legacy_backfill`` for one derived from a legacy ``squad_members``
    operator row that resolved to exactly one active Persona in the Pod's Org.
    A legacy operator membership that does not resolve is left in
    ``squad_members`` and reported as a legacy member rather than invented
    here.
    """

    __tablename__ = "pod_members"
    __table_args__ = (UniqueConstraint("pod_id", "persona_id", name="uq_pod_member_persona"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pod_id: Mapped[int] = mapped_column(ForeignKey("squads.id"), index=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)
    role: Mapped[str] = mapped_column(String(64), default="member")
    source: Mapped[str] = mapped_column(String(24), default="api")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class UsageAttribution(Base):
    """The link between one gateway usage row and the work it was spent on.

    ``usage_ledger`` records install-wide gateway calls and carries no product
    attribution. A caller that identifies its Session (the ``X-Brains-Session``
    request header) gets exactly one row here, so an Issue rollup can sum real
    persisted cost instead of estimating it.

    ``usage_entry_id`` is unique: a retried write attributes the same ledger
    row once, so a rollup can never double-count it. Calls that do not identify
    a Session are simply absent, and the rollup says so rather than implying
    that the Issue cost nothing.
    """

    __tablename__ = "usage_attributions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usage_entry_id: Mapped[int] = mapped_column(
        ForeignKey("usage_ledger.id"), unique=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True, index=True)
    persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True, index=True
    )
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class OnboardingAttempt(Base):
    """One operator's durable run through fresh-state onboarding (BL-P1-04).

    Onboarding is a sequence of real product writes, not a wizard's local
    state: a browser reload, a new tab, or a machine that never connects must
    all resume the same attempt rather than start a new one or claim a success
    that never happened.

    ``status`` is ``in_progress`` while steps remain, ``completed`` only when a
    real Session exists for the Issue the attempt created, and ``blocked`` with
    an explicit ``blocked_reason`` when it cannot get there - a deferred
    machine, an offline Runtime, a refused dispatch. There is no state that
    reports completion without a Session.
    """

    __tablename__ = "onboarding_attempts"
    __table_args__ = (Index("ix_onboarding_attempts_operator_status", "operator_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True, index=True
    )
    org_id: Mapped[int | None] = mapped_column(ForeignKey("orgs.id"), nullable=True, index=True)
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtimes.id"), nullable=True, index=True
    )
    persona_id: Mapped[int | None] = mapped_column(
        ForeignKey("personas.id"), nullable=True, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_sessions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="in_progress", index=True)
    current_step: Mapped[str] = mapped_column(String(24), default="org")
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingStep(Base):
    """The durable outcome of one onboarding step (BL-P1-04).

    ``(attempt_id, step)`` is unique, so retrying a step updates the same row
    and increments ``attempts`` instead of appending a second history. A
    deliberately skipped machine is ``deferred`` - a real, resumable outcome -
    and a step that failed keeps its ``error`` so the console can offer a
    recovery action rather than a dead end.
    """

    __tablename__ = "onboarding_steps"
    __table_args__ = (UniqueConstraint("attempt_id", "step", name="uq_onboarding_attempt_step"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_attempts.attempt_id"), index=True
    )
    step: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    entity_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
