-- Interest-scoped topic delivery and durable cursors (PostgreSQL).

CREATE TABLE IF NOT EXISTS topic_announcements (
	post_id INTEGER NOT NULL,
	excluded_workspace_id INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (post_id),
	FOREIGN KEY(post_id) REFERENCES topic_posts(id),
	FOREIGN KEY(excluded_workspace_id) REFERENCES workspaces(id)
);

CREATE INDEX IF NOT EXISTS ix_topic_announcements_excluded_workspace_id
	ON topic_announcements (excluded_workspace_id);
CREATE INDEX IF NOT EXISTS ix_topic_announcements_created_at
	ON topic_announcements (created_at);

CREATE TABLE IF NOT EXISTS topic_subscriptions (
	session_id VARCHAR(32) NOT NULL,
	topic VARCHAR(64) NOT NULL,
	last_seen_post_id INTEGER NOT NULL DEFAULT 0,
	subscribed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (session_id, topic),
	FOREIGN KEY(session_id) REFERENCES agent_sessions(id)
);

CREATE INDEX IF NOT EXISTS ix_topic_subscriptions_topic
	ON topic_subscriptions (topic);
CREATE INDEX IF NOT EXISTS ix_topic_subscriptions_updated_at
	ON topic_subscriptions (updated_at);
