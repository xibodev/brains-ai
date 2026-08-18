-- Brains frozen baseline schema for the postgresql backend.
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

-- @baseline-block: table=traces
CREATE TABLE IF NOT EXISTS traces (
	id SERIAL NOT NULL, 
	route VARCHAR(64) NOT NULL, 
	payload TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

-- @baseline-block: table=route_decisions
CREATE TABLE IF NOT EXISTS route_decisions (
	id SERIAL NOT NULL, 
	task_type VARCHAR(64) NOT NULL, 
	model_tier VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

-- @baseline-block: table=memories
CREATE TABLE IF NOT EXISTS memories (
	id SERIAL NOT NULL, 
	key VARCHAR(128) NOT NULL, 
	value TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_memories_key ON memories (key);

-- @baseline-block: table=freshness_checks
CREATE TABLE IF NOT EXISTS freshness_checks (
	id SERIAL NOT NULL, 
	source VARCHAR(512) NOT NULL, 
	check_type VARCHAR(32) NOT NULL, 
	metadata TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

-- @baseline-block: table=operators
CREATE TABLE IF NOT EXISTS operators (
	id SERIAL NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128), 
	key_fingerprint VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_operators_key_fingerprint ON operators (key_fingerprint);

CREATE UNIQUE INDEX IF NOT EXISTS ix_operators_slug ON operators (slug);

-- @baseline-block: table=agent_sessions
CREATE TABLE IF NOT EXISTS agent_sessions (
	id VARCHAR(32) NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	tool VARCHAR(64) NOT NULL, 
	pid INTEGER, 
	machine_id VARCHAR(64), 
	created_by_operator_id INTEGER, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	ended_at TIMESTAMP WITH TIME ZONE, 
	state VARCHAR(16) NOT NULL, 
	last_activity_at TIMESTAMP WITH TIME ZONE, 
	summary TEXT, 
	metadata_json TEXT, 
	issue_id INTEGER, 
	persona_id INTEGER, 
	runtime_id INTEGER, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_created_by_operator_id ON agent_sessions (created_by_operator_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_issue_id ON agent_sessions (issue_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_activity_at ON agent_sessions (last_activity_at);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_machine_id ON agent_sessions (machine_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_persona_id ON agent_sessions (persona_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_runtime_id ON agent_sessions (runtime_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_state ON agent_sessions (state);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_workspace_id ON agent_sessions (workspace_id);

CREATE INDEX IF NOT EXISTS ix_agent_sessions_ws_activity ON agent_sessions (workspace_id, last_activity_at);

-- @baseline-block: table=registered_tools
CREATE TABLE IF NOT EXISTS registered_tools (
	name VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	cli_command VARCHAR(512) NOT NULL, 
	spawn_args TEXT, 
	capabilities TEXT, 
	installed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_verified_at TIMESTAMP WITH TIME ZONE, 
	is_available INTEGER NOT NULL, 
	notes TEXT, 
	PRIMARY KEY (name)
);

-- @baseline-block: table=chunks_meta
CREATE TABLE IF NOT EXISTS chunks_meta (
	id INTEGER NOT NULL, 
	embed_model VARCHAR(128) NOT NULL, 
	embed_dim INTEGER NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

-- @baseline-block: table=usage_ledger
CREATE TABLE IF NOT EXISTS usage_ledger (
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE NOT NULL, 
	endpoint VARCHAR(64) NOT NULL, 
	requested_model VARCHAR(128) NOT NULL, 
	routed_model VARCHAR(128) NOT NULL, 
	provider VARCHAR(64) NOT NULL, 
	task_type VARCHAR(64), 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	cost_actual_usd FLOAT, 
	cost_baseline_usd FLOAT, 
	savings_usd FLOAT, 
	is_stub BOOLEAN DEFAULT '0' NOT NULL, 
	is_holdout BOOLEAN DEFAULT '0' NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_endpoint ON usage_ledger (endpoint);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_is_holdout ON usage_ledger (is_holdout);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_is_stub ON usage_ledger (is_stub);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_provider ON usage_ledger (provider);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_routed_model ON usage_ledger (routed_model);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_savings_usd ON usage_ledger (savings_usd);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_task_type ON usage_ledger (task_type);

CREATE INDEX IF NOT EXISTS ix_usage_ledger_ts ON usage_ledger (ts);

-- @baseline-block: table=squads
CREATE TABLE IF NOT EXISTS squads (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	leader_operator_id INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_by_session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	archived_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_squad_workspace_slug UNIQUE (workspace_id, slug)
);

CREATE INDEX IF NOT EXISTS ix_squads_leader_operator_id ON squads (leader_operator_id);

CREATE INDEX IF NOT EXISTS ix_squads_slug ON squads (slug);

CREATE INDEX IF NOT EXISTS ix_squads_status ON squads (status);

CREATE INDEX IF NOT EXISTS ix_squads_workspace_id ON squads (workspace_id);

-- @baseline-block: table=webhook_triggers
CREATE TABLE IF NOT EXISTS webhook_triggers (
	id SERIAL NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	definition_name VARCHAR(128) NOT NULL, 
	token_hash VARCHAR(128) NOT NULL, 
	event_filter VARCHAR(256), 
	enabled BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_webhook_triggers_definition_name ON webhook_triggers (definition_name);

CREATE UNIQUE INDEX IF NOT EXISTS ix_webhook_triggers_slug ON webhook_triggers (slug);

-- @baseline-block: table=orgs
CREATE TABLE IF NOT EXISTS orgs (
	id SERIAL NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_orgs_slug ON orgs (slug);

CREATE INDEX IF NOT EXISTS ix_orgs_status ON orgs (status);

-- @baseline-block: table=personas
CREATE TABLE IF NOT EXISTS personas (
	id SERIAL NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	org_id INTEGER NOT NULL, 
	operator_id INTEGER, 
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	system_prompt TEXT, 
	model VARCHAR(64), 
	tool VARCHAR(64), 
	default_runtime_id INTEGER, 
	color VARCHAR(16), 
	avatar VARCHAR(256), 
	status VARCHAR(16) NOT NULL, 
	created_by_session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_persona_org_slug UNIQUE (org_id, slug)
);

CREATE INDEX IF NOT EXISTS ix_personas_default_runtime_id ON personas (default_runtime_id);

CREATE INDEX IF NOT EXISTS ix_personas_operator_id ON personas (operator_id);

CREATE INDEX IF NOT EXISTS ix_personas_org_id ON personas (org_id);

CREATE INDEX IF NOT EXISTS ix_personas_slug ON personas (slug);

CREATE INDEX IF NOT EXISTS ix_personas_status ON personas (status);

-- @baseline-block: table=projects
CREATE TABLE IF NOT EXISTS projects (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	org_id INTEGER NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(256) NOT NULL, 
	description TEXT, 
	workspace_id INTEGER, 
	status VARCHAR(16) NOT NULL, 
	assignee_pod_id INTEGER, 
	created_by_session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_project_org_slug UNIQUE (org_id, slug)
);

CREATE INDEX IF NOT EXISTS ix_projects_assignee_pod_id ON projects (assignee_pod_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_projects_code ON projects (code);

CREATE INDEX IF NOT EXISTS ix_projects_org_id ON projects (org_id);

CREATE INDEX IF NOT EXISTS ix_projects_slug ON projects (slug);

CREATE INDEX IF NOT EXISTS ix_projects_status ON projects (status);

CREATE INDEX IF NOT EXISTS ix_projects_workspace_id ON projects (workspace_id);

-- @baseline-block: table=issues
CREATE TABLE IF NOT EXISTS issues (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	project_id INTEGER NOT NULL, 
	workspace_id INTEGER, 
	parent_issue_id INTEGER, 
	title VARCHAR(256) NOT NULL, 
	body TEXT, 
	status VARCHAR(24) NOT NULL, 
	priority VARCHAR(16) NOT NULL, 
	assignee_persona_id INTEGER, 
	assignee_pod_id INTEGER, 
	assignee_operator_id INTEGER, 
	agent_task_code VARCHAR(32), 
	labels TEXT, 
	created_by_session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_issues_assignee_operator_id ON issues (assignee_operator_id);

CREATE INDEX IF NOT EXISTS ix_issues_assignee_persona_id ON issues (assignee_persona_id);

CREATE INDEX IF NOT EXISTS ix_issues_assignee_pod_id ON issues (assignee_pod_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_code ON issues (code);

CREATE INDEX IF NOT EXISTS ix_issues_created_at ON issues (created_at);

CREATE INDEX IF NOT EXISTS ix_issues_priority ON issues (priority);

CREATE INDEX IF NOT EXISTS ix_issues_project_id ON issues (project_id);

CREATE INDEX IF NOT EXISTS ix_issues_project_status ON issues (project_id, status);

CREATE INDEX IF NOT EXISTS ix_issues_status ON issues (status);

CREATE INDEX IF NOT EXISTS ix_issues_workspace_id ON issues (workspace_id);

-- @baseline-block: table=workspaces
CREATE TABLE IF NOT EXISTS workspaces (
	id SERIAL NOT NULL, 
	slug VARCHAR(128) NOT NULL, 
	path VARCHAR(1024) NOT NULL, 
	name VARCHAR(256), 
	status VARCHAR(32) NOT NULL, 
	visibility VARCHAR(16) NOT NULL, 
	org_id INTEGER, 
	last_touched_at TIMESTAMP WITH TIME ZONE, 
	last_summary TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES orgs (id)
);

CREATE INDEX IF NOT EXISTS ix_workspaces_org_id ON workspaces (org_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_workspaces_path ON workspaces (path);

CREATE UNIQUE INDEX IF NOT EXISTS ix_workspaces_slug ON workspaces (slug);

-- @baseline-block: table=knowledge_patterns
CREATE TABLE IF NOT EXISTS knowledge_patterns (
	id SERIAL NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	description TEXT NOT NULL, 
	example TEXT, 
	applies_to TEXT, 
	status VARCHAR(32) NOT NULL, 
	proposed_by_session_id VARCHAR(32), 
	proposed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	approved_at TIMESTAMP WITH TIME ZONE, 
	usage_count INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proposed_by_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_knowledge_patterns_category ON knowledge_patterns (category);

CREATE UNIQUE INDEX IF NOT EXISTS ix_knowledge_patterns_name ON knowledge_patterns (name);

CREATE INDEX IF NOT EXISTS ix_knowledge_patterns_proposed_by_session_id ON knowledge_patterns (proposed_by_session_id);

CREATE INDEX IF NOT EXISTS ix_knowledge_patterns_status ON knowledge_patterns (status);

-- @baseline-block: table=tool_session_links
CREATE TABLE IF NOT EXISTS tool_session_links (
	id SERIAL NOT NULL, 
	brain_session_id VARCHAR(32) NOT NULL, 
	tool VARCHAR(64) NOT NULL, 
	tool_session_id VARCHAR(256) NOT NULL, 
	linked_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	linked_by VARCHAR(16) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brain_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_tool_session_links_brain_session_id ON tool_session_links (brain_session_id);

CREATE INDEX IF NOT EXISTS ix_tool_session_links_tool ON tool_session_links (tool);

CREATE INDEX IF NOT EXISTS ix_tool_session_links_tool_session_id ON tool_session_links (tool_session_id);

-- @baseline-block: table=squad_members
CREATE TABLE IF NOT EXISTS squad_members (
	id SERIAL NOT NULL, 
	squad_id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	role VARCHAR(64) NOT NULL, 
	added_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_squad_member UNIQUE (squad_id, operator_id), 
	FOREIGN KEY(squad_id) REFERENCES squads (id), 
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);

CREATE INDEX IF NOT EXISTS ix_squad_members_operator_id ON squad_members (operator_id);

CREATE INDEX IF NOT EXISTS ix_squad_members_squad_id ON squad_members (squad_id);

-- @baseline-block: table=webhook_deliveries
CREATE TABLE IF NOT EXISTS webhook_deliveries (
	id SERIAL NOT NULL, 
	trigger_id INTEGER NOT NULL, 
	dedupe_key VARCHAR(128) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	task_code VARCHAR(32), 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_webhook_delivery UNIQUE (trigger_id, dedupe_key), 
	FOREIGN KEY(trigger_id) REFERENCES webhook_triggers (id)
);

CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_trigger_id ON webhook_deliveries (trigger_id);

-- @baseline-block: table=org_members
CREATE TABLE IF NOT EXISTS org_members (
	id SERIAL NOT NULL, 
	org_id INTEGER NOT NULL, 
	operator_id INTEGER NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_org_member UNIQUE (org_id, operator_id), 
	FOREIGN KEY(org_id) REFERENCES orgs (id), 
	FOREIGN KEY(operator_id) REFERENCES operators (id)
);

CREATE INDEX IF NOT EXISTS ix_org_members_operator_id ON org_members (operator_id);

CREATE INDEX IF NOT EXISTS ix_org_members_org_id ON org_members (org_id);

-- @baseline-block: table=runtimes
CREATE TABLE IF NOT EXISTS runtimes (
	id SERIAL NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	org_id INTEGER, 
	machine_id VARCHAR(64) NOT NULL, 
	machine_label VARCHAR(128), 
	tool VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128), 
	daemon_version VARCHAR(32), 
	os VARCHAR(32), 
	working_root VARCHAR(1024), 
	capabilities TEXT, 
	status VARCHAR(16) NOT NULL, 
	health VARCHAR(16) NOT NULL, 
	last_heartbeat_at TIMESTAMP WITH TIME ZONE, 
	registered_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	metadata_json TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_runtime_machine_tool UNIQUE (machine_id, tool), 
	FOREIGN KEY(org_id) REFERENCES orgs (id), 
	FOREIGN KEY(tool) REFERENCES registered_tools (name)
);

CREATE INDEX IF NOT EXISTS ix_runtimes_last_heartbeat_at ON runtimes (last_heartbeat_at);

CREATE INDEX IF NOT EXISTS ix_runtimes_machine_id ON runtimes (machine_id);

CREATE INDEX IF NOT EXISTS ix_runtimes_org_id ON runtimes (org_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_runtimes_slug ON runtimes (slug);

CREATE INDEX IF NOT EXISTS ix_runtimes_status ON runtimes (status);

CREATE INDEX IF NOT EXISTS ix_runtimes_tool ON runtimes (tool);

-- @baseline-block: table=enrolment_tokens
CREATE TABLE IF NOT EXISTS enrolment_tokens (
	id SERIAL NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	label VARCHAR(128), 
	org_id INTEGER, 
	created_by_operator_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	redeemed_at TIMESTAMP WITH TIME ZONE, 
	redeemed_machine_id VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES orgs (id)
);

CREATE INDEX IF NOT EXISTS ix_enrolment_tokens_org_id ON enrolment_tokens (org_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_enrolment_tokens_token_hash ON enrolment_tokens (token_hash);

-- @baseline-block: table=issue_comments
CREATE TABLE IF NOT EXISTS issue_comments (
	id SERIAL NOT NULL, 
	issue_id INTEGER NOT NULL, 
	body TEXT NOT NULL, 
	author_kind VARCHAR(16) NOT NULL, 
	author_operator_id INTEGER, 
	author_persona_id INTEGER, 
	session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issues (id), 
	FOREIGN KEY(author_operator_id) REFERENCES operators (id), 
	FOREIGN KEY(author_persona_id) REFERENCES personas (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_issue_comments_created_at ON issue_comments (created_at);

CREATE INDEX IF NOT EXISTS ix_issue_comments_issue_created ON issue_comments (issue_id, created_at);

CREATE INDEX IF NOT EXISTS ix_issue_comments_issue_id ON issue_comments (issue_id);

-- @baseline-block: table=skills
CREATE TABLE IF NOT EXISTS skills (
	id SERIAL NOT NULL, 
	org_id INTEGER, 
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	content TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES orgs (id)
);

CREATE INDEX IF NOT EXISTS ix_skills_created_at ON skills (created_at);

CREATE INDEX IF NOT EXISTS ix_skills_org_id ON skills (org_id);

CREATE INDEX IF NOT EXISTS ix_skills_org_slug ON skills (org_id, slug);

-- @baseline-block: table=workspace_memberships
CREATE TABLE IF NOT EXISTS workspace_memberships (
	id SERIAL NOT NULL, 
	operator_id INTEGER NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_workspace_memberships_op_ws UNIQUE (operator_id, workspace_id), 
	FOREIGN KEY(operator_id) REFERENCES operators (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_workspace_memberships_operator_id ON workspace_memberships (operator_id);

CREATE INDEX IF NOT EXISTS ix_workspace_memberships_workspace_id ON workspace_memberships (workspace_id);

-- @baseline-block: table=events
CREATE TABLE IF NOT EXISTS events (
	id SERIAL NOT NULL, 
	workspace_id INTEGER, 
	session_id VARCHAR(32), 
	kind VARCHAR(64) NOT NULL, 
	message TEXT NOT NULL, 
	metadata_json TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_events_created_at ON events (created_at);

CREATE INDEX IF NOT EXISTS ix_events_kind ON events (kind);

CREATE INDEX IF NOT EXISTS ix_events_session_id ON events (session_id);

CREATE INDEX IF NOT EXISTS ix_events_workspace_id ON events (workspace_id);

CREATE INDEX IF NOT EXISTS ix_events_ws_created ON events (workspace_id, created_at);

-- @baseline-block: table=approval_requests
CREATE TABLE IF NOT EXISTS approval_requests (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	session_id VARCHAR(32), 
	title VARCHAR(256) NOT NULL, 
	body TEXT, 
	body_path VARCHAR(1024), 
	proposed_answer TEXT, 
	status VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	decision_id INTEGER, 
	metadata_json TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_approval_requests_code ON approval_requests (code);

CREATE INDEX IF NOT EXISTS ix_approval_requests_session_id ON approval_requests (session_id);

CREATE INDEX IF NOT EXISTS ix_approval_requests_status ON approval_requests (status);

CREATE INDEX IF NOT EXISTS ix_approval_requests_workspace_id ON approval_requests (workspace_id);

-- @baseline-block: table=handoffs
CREATE TABLE IF NOT EXISTS handoffs (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	title VARCHAR(256) NOT NULL, 
	body TEXT, 
	set_by_session_id VARCHAR(32), 
	set_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	picked_up_by_session_id VARCHAR(32), 
	picked_up_at TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(set_by_session_id) REFERENCES agent_sessions (id), 
	FOREIGN KEY(picked_up_by_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_handoffs_status ON handoffs (status);

CREATE INDEX IF NOT EXISTS ix_handoffs_workspace_id ON handoffs (workspace_id);

-- @baseline-block: table=agent_tasks
CREATE TABLE IF NOT EXISTS agent_tasks (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	title VARCHAR(256) NOT NULL, 
	body TEXT, 
	priority VARCHAR(16) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_by_session_id VARCHAR(32), 
	claimed_by_session_id VARCHAR(32), 
	claimed_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	completion_summary TEXT, 
	depends_on TEXT, 
	tags TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id), 
	FOREIGN KEY(claimed_by_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_claimed_by_session_id ON agent_tasks (claimed_by_session_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_tasks_code ON agent_tasks (code);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_created_by_session_id ON agent_tasks (created_by_session_id);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_priority ON agent_tasks (priority);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_status ON agent_tasks (status);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_workspace_id ON agent_tasks (workspace_id);

CREATE INDEX IF NOT EXISTS ix_agent_tasks_ws_status ON agent_tasks (workspace_id, status);

-- @baseline-block: table=workspace_claims
CREATE TABLE IF NOT EXISTS workspace_claims (
	workspace_id INTEGER NOT NULL, 
	session_id VARCHAR(32) NOT NULL, 
	scope VARCHAR(64) NOT NULL, 
	claimed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	metadata_json TEXT, 
	PRIMARY KEY (workspace_id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_workspace_claims_expires_at ON workspace_claims (expires_at);

CREATE INDEX IF NOT EXISTS ix_workspace_claims_session_id ON workspace_claims (session_id);

-- @baseline-block: table=mailbox_messages
CREATE TABLE IF NOT EXISTS mailbox_messages (
	id SERIAL NOT NULL, 
	workspace_id INTEGER, 
	from_session_id VARCHAR(32), 
	to_session_id VARCHAR(32), 
	kind VARCHAR(32) NOT NULL, 
	subject VARCHAR(256) NOT NULL, 
	body TEXT, 
	read_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_mailbox_messages_created_at ON mailbox_messages (created_at);

CREATE INDEX IF NOT EXISTS ix_mailbox_messages_kind ON mailbox_messages (kind);

CREATE INDEX IF NOT EXISTS ix_mailbox_messages_read_at ON mailbox_messages (read_at);

CREATE INDEX IF NOT EXISTS ix_mailbox_messages_to_session_id ON mailbox_messages (to_session_id);

CREATE INDEX IF NOT EXISTS ix_mailbox_messages_workspace_id ON mailbox_messages (workspace_id);

-- @baseline-block: table=snapshots
CREATE TABLE IF NOT EXISTS snapshots (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	kind VARCHAR(64) NOT NULL, 
	data_json TEXT NOT NULL, 
	captured_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_snapshots_captured_at ON snapshots (captured_at);

CREATE INDEX IF NOT EXISTS ix_snapshots_kind ON snapshots (kind);

CREATE INDEX IF NOT EXISTS ix_snapshots_workspace_id ON snapshots (workspace_id);

-- @baseline-block: table=recurring_task_definitions
CREATE TABLE IF NOT EXISTS recurring_task_definitions (
	id SERIAL NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	title_template VARCHAR(256) NOT NULL, 
	body_template TEXT, 
	priority VARCHAR(16) NOT NULL, 
	tags TEXT, 
	cron_expr VARCHAR(64) NOT NULL, 
	enabled INTEGER NOT NULL, 
	last_fired_at TIMESTAMP WITH TIME ZONE, 
	spawn_tool TEXT, 
	spawn_args TEXT, 
	spawn_prompt TEXT, 
	squad TEXT, 
	created_by_session_id VARCHAR(32), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_recurring_task_definitions_created_by_session_id ON recurring_task_definitions (created_by_session_id);

CREATE INDEX IF NOT EXISTS ix_recurring_task_definitions_enabled ON recurring_task_definitions (enabled);

CREATE UNIQUE INDEX IF NOT EXISTS ix_recurring_task_definitions_name ON recurring_task_definitions (name);

CREATE INDEX IF NOT EXISTS ix_recurring_task_definitions_workspace_id ON recurring_task_definitions (workspace_id);

-- @baseline-block: table=sources
CREATE TABLE IF NOT EXISTS sources (
	id SERIAL NOT NULL, 
	workspace_id INTEGER, 
	source_type VARCHAR(32) NOT NULL, 
	uri VARCHAR(1024) NOT NULL, 
	title VARCHAR(256), 
	status VARCHAR(32) NOT NULL, 
	metadata_json TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_sources_source_type ON sources (source_type);

CREATE INDEX IF NOT EXISTS ix_sources_uri ON sources (uri);

CREATE INDEX IF NOT EXISTS ix_sources_workspace_id ON sources (workspace_id);

-- @baseline-block: table=code_graph_nodes
CREATE TABLE IF NOT EXISTS code_graph_nodes (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	name VARCHAR(512) NOT NULL, 
	path VARCHAR(1024) NOT NULL, 
	lineno INTEGER, 
	subsystem_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_code_graph_nodes_ws_path_kind_name UNIQUE (workspace_id, path, kind, name), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_path ON code_graph_nodes (path);

CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_subsystem_id ON code_graph_nodes (subsystem_id);

CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_workspace_id ON code_graph_nodes (workspace_id);

-- @baseline-block: table=help_requests
CREATE TABLE IF NOT EXISTS help_requests (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	from_session_id VARCHAR(32), 
	from_workspace_id INTEGER, 
	to_workspace VARCHAR(128), 
	to_session_id VARCHAR(32), 
	subject VARCHAR(256) NOT NULL, 
	question TEXT NOT NULL, 
	context TEXT, 
	status VARCHAR(32) NOT NULL, 
	claimed_by_session_id VARCHAR(32), 
	claimed_at TIMESTAMP WITH TIME ZONE, 
	answer TEXT, 
	evidence TEXT, 
	answered_at TIMESTAMP WITH TIME ZONE, 
	ask_depth INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(from_session_id) REFERENCES agent_sessions (id), 
	FOREIGN KEY(from_workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(claimed_by_session_id) REFERENCES agent_sessions (id)
);

CREATE INDEX IF NOT EXISTS ix_help_requests_claimed_by_session_id ON help_requests (claimed_by_session_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_help_requests_code ON help_requests (code);

CREATE INDEX IF NOT EXISTS ix_help_requests_created_at ON help_requests (created_at);

CREATE INDEX IF NOT EXISTS ix_help_requests_expires_at ON help_requests (expires_at);

CREATE INDEX IF NOT EXISTS ix_help_requests_from_session_id ON help_requests (from_session_id);

CREATE INDEX IF NOT EXISTS ix_help_requests_from_workspace_id ON help_requests (from_workspace_id);

CREATE INDEX IF NOT EXISTS ix_help_requests_status ON help_requests (status);

CREATE INDEX IF NOT EXISTS ix_help_requests_to_session_id ON help_requests (to_session_id);

CREATE INDEX IF NOT EXISTS ix_help_requests_to_workspace ON help_requests (to_workspace);

-- @baseline-block: table=session_checkpoints
CREATE TABLE IF NOT EXISTS session_checkpoints (
	id SERIAL NOT NULL, 
	session_id VARCHAR(32) NOT NULL, 
	workspace_id INTEGER, 
	summary TEXT NOT NULL, 
	next_action TEXT, 
	blockers TEXT, 
	scratchpad_path TEXT, 
	metadata_json TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_session_checkpoints_created_at ON session_checkpoints (created_at);

CREATE INDEX IF NOT EXISTS ix_session_checkpoints_session_id ON session_checkpoints (session_id);

CREATE INDEX IF NOT EXISTS ix_session_checkpoints_workspace_id ON session_checkpoints (workspace_id);

-- @baseline-block: table=audit_log
CREATE TABLE IF NOT EXISTS audit_log (
	id SERIAL NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	action VARCHAR(64) NOT NULL, 
	workspace_id INTEGER, 
	payload_json TEXT NOT NULL, 
	prev_hash VARCHAR(64) NOT NULL, 
	entry_hash VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	UNIQUE (entry_hash)
);

CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log (action);

CREATE INDEX IF NOT EXISTS ix_audit_log_actor ON audit_log (actor);

CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at);

-- @baseline-block: table=recurring_runs
CREATE TABLE IF NOT EXISTS recurring_runs (
	id SERIAL NOT NULL, 
	definition_name VARCHAR(128) NOT NULL, 
	workspace_id INTEGER, 
	source VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	task_code VARCHAR(32), 
	trigger_payload TEXT, 
	failure_reason TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_created_at ON recurring_runs (created_at);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_def ON recurring_runs (definition_name, created_at);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_definition_name ON recurring_runs (definition_name);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_source ON recurring_runs (source);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_status ON recurring_runs (status);

CREATE INDEX IF NOT EXISTS ix_recurring_runs_workspace_id ON recurring_runs (workspace_id);

-- @baseline-block: table=approval_decisions
CREATE TABLE IF NOT EXISTS approval_decisions (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	approval_request_id INTEGER NOT NULL, 
	chosen TEXT NOT NULL, 
	reasoning TEXT, 
	decided_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	metadata_json TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(approval_request_id) REFERENCES approval_requests (id)
);

CREATE INDEX IF NOT EXISTS ix_approval_decisions_approval_request_id ON approval_decisions (approval_request_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_approval_decisions_code ON approval_decisions (code);

-- @baseline-block: table=artifacts
CREATE TABLE IF NOT EXISTS artifacts (
	id SERIAL NOT NULL, 
	source_id INTEGER NOT NULL, 
	path VARCHAR(1024) NOT NULL, 
	language VARCHAR(64), 
	size INTEGER NOT NULL, 
	hash VARCHAR(128), 
	mtime TIMESTAMP WITH TIME ZONE, 
	title VARCHAR(256), 
	summary TEXT, 
	metadata_json TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_id) REFERENCES sources (id)
);

CREATE INDEX IF NOT EXISTS ix_artifacts_path ON artifacts (path);

CREATE INDEX IF NOT EXISTS ix_artifacts_source_id ON artifacts (source_id);

-- @baseline-block: table=code_graph_edges
CREATE TABLE IF NOT EXISTS code_graph_edges (
	id SERIAL NOT NULL, 
	workspace_id INTEGER NOT NULL, 
	src_id INTEGER NOT NULL, 
	dst_id INTEGER NOT NULL, 
	relation VARCHAR(16) NOT NULL, 
	confidence VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(src_id) REFERENCES code_graph_nodes (id), 
	FOREIGN KEY(dst_id) REFERENCES code_graph_nodes (id)
);

CREATE INDEX IF NOT EXISTS ix_code_graph_edges_dst_id ON code_graph_edges (dst_id);

CREATE INDEX IF NOT EXISTS ix_code_graph_edges_src_id ON code_graph_edges (src_id);

CREATE INDEX IF NOT EXISTS ix_code_graph_edges_workspace_id ON code_graph_edges (workspace_id);

-- @baseline-block: table=knowledge_entries
CREATE TABLE IF NOT EXISTS knowledge_entries (
	id SERIAL NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	body TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	scope VARCHAR(16) NOT NULL, 
	workspace_id INTEGER, 
	tags VARCHAR(300) NOT NULL, 
	confidence VARCHAR(16) NOT NULL, 
	provenance VARCHAR(16) NOT NULL, 
	importance FLOAT NOT NULL, 
	severity VARCHAR(16) NOT NULL, 
	valid_until TIMESTAMP WITH TIME ZONE, 
	created_by_operator_id INTEGER, 
	created_by_session_id VARCHAR(32), 
	source_event_id INTEGER, 
	promoted_from INTEGER, 
	superseded_by_id INTEGER, 
	evidence TEXT NOT NULL, 
	metadata_json TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id), 
	FOREIGN KEY(created_by_operator_id) REFERENCES operators (id), 
	FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id), 
	FOREIGN KEY(source_event_id) REFERENCES events (id), 
	FOREIGN KEY(promoted_from) REFERENCES knowledge_entries (id), 
	FOREIGN KEY(superseded_by_id) REFERENCES knowledge_entries (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_knowledge_entries_code ON knowledge_entries (code);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_created_at ON knowledge_entries (created_at);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_created_by_operator_id ON knowledge_entries (created_by_operator_id);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_created_by_session_id ON knowledge_entries (created_by_session_id);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_importance ON knowledge_entries (importance);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_scope ON knowledge_entries (scope);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_status ON knowledge_entries (status);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_type ON knowledge_entries (type);

CREATE INDEX IF NOT EXISTS ix_knowledge_entries_workspace_id ON knowledge_entries (workspace_id);

CREATE INDEX IF NOT EXISTS ix_knowledge_ws_status ON knowledge_entries (workspace_id, status);

-- @baseline-block: table=chunks
CREATE TABLE IF NOT EXISTS chunks (
	id SERIAL NOT NULL, 
	artifact_id INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	token_estimate INTEGER, 
	hash VARCHAR(128), 
	metadata_json TEXT, 
	embedding BYTEA, 
	PRIMARY KEY (id), 
	FOREIGN KEY(artifact_id) REFERENCES artifacts (id)
);

CREATE INDEX IF NOT EXISTS ix_chunks_artifact_id ON chunks (artifact_id);

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('agent_sessions')
          AND c.confrelid = to_regclass('operators')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'created_by_operator_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE agent_sessions ADD CONSTRAINT fk_agent_sessions_created_by_operator_id_operators FOREIGN KEY(created_by_operator_id) REFERENCES operators (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('agent_sessions')
          AND c.confrelid = to_regclass('issues')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'issue_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE agent_sessions ADD CONSTRAINT fk_agent_sessions_issue_id_issues FOREIGN KEY(issue_id) REFERENCES issues (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('agent_sessions')
          AND c.confrelid = to_regclass('personas')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'persona_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE agent_sessions ADD CONSTRAINT fk_agent_sessions_persona_id_personas FOREIGN KEY(persona_id) REFERENCES personas (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('agent_sessions')
          AND c.confrelid = to_regclass('runtimes')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'runtime_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE agent_sessions ADD CONSTRAINT fk_agent_sessions_runtime_id_runtimes FOREIGN KEY(runtime_id) REFERENCES runtimes (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('agent_sessions')
          AND c.confrelid = to_regclass('workspaces')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'workspace_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE agent_sessions ADD CONSTRAINT fk_agent_sessions_workspace_id_workspaces FOREIGN KEY(workspace_id) REFERENCES workspaces (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('operators')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'assignee_operator_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_assignee_operator_id_operators FOREIGN KEY(assignee_operator_id) REFERENCES operators (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('personas')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'assignee_persona_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_assignee_persona_id_personas FOREIGN KEY(assignee_persona_id) REFERENCES personas (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('squads')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'assignee_pod_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_assignee_pod_id_squads FOREIGN KEY(assignee_pod_id) REFERENCES squads (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('agent_sessions')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'created_by_session_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_created_by_session_id_agent_sessions FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('issues')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'parent_issue_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_parent_issue_id_issues FOREIGN KEY(parent_issue_id) REFERENCES issues (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('projects')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'project_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('issues')
          AND c.confrelid = to_regclass('workspaces')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'workspace_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE issues ADD CONSTRAINT fk_issues_workspace_id_workspaces FOREIGN KEY(workspace_id) REFERENCES workspaces (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('personas')
          AND c.confrelid = to_regclass('agent_sessions')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'created_by_session_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE personas ADD CONSTRAINT fk_personas_created_by_session_id_agent_sessions FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('personas')
          AND c.confrelid = to_regclass('runtimes')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'default_runtime_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE personas ADD CONSTRAINT fk_personas_default_runtime_id_runtimes FOREIGN KEY(default_runtime_id) REFERENCES runtimes (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('personas')
          AND c.confrelid = to_regclass('operators')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'operator_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE personas ADD CONSTRAINT fk_personas_operator_id_operators FOREIGN KEY(operator_id) REFERENCES operators (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('personas')
          AND c.confrelid = to_regclass('orgs')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'org_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE personas ADD CONSTRAINT fk_personas_org_id_orgs FOREIGN KEY(org_id) REFERENCES orgs (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('personas')
          AND c.confrelid = to_regclass('registered_tools')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'tool')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'name')
              ]::smallint[]
    ) THEN
        ALTER TABLE personas ADD CONSTRAINT fk_personas_tool_registered_tools FOREIGN KEY(tool) REFERENCES registered_tools (name);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('projects')
          AND c.confrelid = to_regclass('squads')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'assignee_pod_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE projects ADD CONSTRAINT fk_projects_assignee_pod_id_squads FOREIGN KEY(assignee_pod_id) REFERENCES squads (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('projects')
          AND c.confrelid = to_regclass('agent_sessions')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'created_by_session_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE projects ADD CONSTRAINT fk_projects_created_by_session_id_agent_sessions FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('projects')
          AND c.confrelid = to_regclass('orgs')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'org_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE projects ADD CONSTRAINT fk_projects_org_id_orgs FOREIGN KEY(org_id) REFERENCES orgs (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('projects')
          AND c.confrelid = to_regclass('workspaces')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'workspace_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE projects ADD CONSTRAINT fk_projects_workspace_id_workspaces FOREIGN KEY(workspace_id) REFERENCES workspaces (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('squads')
          AND c.confrelid = to_regclass('agent_sessions')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'created_by_session_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE squads ADD CONSTRAINT fk_squads_created_by_session_id_agent_sessions FOREIGN KEY(created_by_session_id) REFERENCES agent_sessions (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('squads')
          AND c.confrelid = to_regclass('operators')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'leader_operator_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE squads ADD CONSTRAINT fk_squads_leader_operator_id_operators FOREIGN KEY(leader_operator_id) REFERENCES operators (id);
    END IF;
END
$$;

-- @baseline-block: always
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid = to_regclass('squads')
          AND c.confrelid = to_regclass('workspaces')
          AND c.conkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attname = 'workspace_id')
              ]::smallint[]
          AND c.confkey = ARRAY[
                  (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attname = 'id')
              ]::smallint[]
    ) THEN
        ALTER TABLE squads ADD CONSTRAINT fk_squads_workspace_id_workspaces FOREIGN KEY(workspace_id) REFERENCES workspaces (id);
    END IF;
END
$$;
