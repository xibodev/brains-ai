"""Phase 8 structural code graph tables.

Fresh databases get these tables from SQLAlchemy ``create_all``. This
migration creates the same SQLite tables/indexes for existing databases.
"""

from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS code_graph_nodes (
            id INTEGER NOT NULL PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            kind VARCHAR(16) NOT NULL,
            name VARCHAR(512) NOT NULL,
            path VARCHAR(1024) NOT NULL,
            lineno INTEGER,
            subsystem_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT uq_code_graph_nodes_ws_path_kind_name
                UNIQUE (workspace_id, path, kind, name),
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS code_graph_edges (
            id INTEGER NOT NULL PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            relation VARCHAR(16) NOT NULL,
            confidence VARCHAR(16) DEFAULT 'extracted' NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id),
            FOREIGN KEY(src_id) REFERENCES code_graph_nodes (id),
            FOREIGN KEY(dst_id) REFERENCES code_graph_nodes (id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_workspace_id "
        "ON code_graph_nodes(workspace_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_path ON code_graph_nodes(path)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_graph_nodes_subsystem_id "
        "ON code_graph_nodes(subsystem_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_graph_edges_workspace_id "
        "ON code_graph_edges(workspace_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_graph_edges_src_id ON code_graph_edges(src_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_code_graph_edges_dst_id ON code_graph_edges(dst_id)"
    )
