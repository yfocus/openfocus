# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

STARTUP_SCHEMA_MIGRATION_ID = "20260511_startup_schema_baseline"


def _table_columns(conn: Any, table_name: str) -> list[str]:
    return [
        r[1]
        for r in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    ]


def _ensure_schema_migrations_table(conn: Any) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id VARCHAR(128) PRIMARY KEY, "
            "applied_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
            ")"
        )
    )


def _mark_applied(conn: Any, migration_id: str) -> None:
    conn.execute(
        text("INSERT OR IGNORE INTO schema_migrations (id) VALUES (:id)"),
        {"id": str(migration_id)},
    )


def initialize_database(engine: Engine, base: Any) -> None:
    """Create the current schema and run OpenFocus startup migrations.

    This is a deliberately small migration runner. It makes existing startup
    schema repair explicit and records a baseline id, while avoiding a partial
    Alembic integration until the project is ready to own full revision files.
    """

    base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _ensure_schema_migrations_table(conn)
        run_startup_schema_migrations(conn)
        _mark_applied(conn, STARTUP_SCHEMA_MIGRATION_ID)


def run_startup_schema_migrations(conn: Any) -> None:
    goals_cols = _table_columns(conn, "goals")
    if "title" not in goals_cols:
        conn.execute(
            text("ALTER TABLE goals ADD COLUMN title VARCHAR(2000) NOT NULL DEFAULT ''")
        )
        conn.execute(text("UPDATE goals SET title = content WHERE title = ''"))
        if "description" in goals_cols:
            conn.execute(
                text(
                    "UPDATE goals SET content = description "
                    "WHERE COALESCE(description, '') != ''"
                )
            )
    if "status" not in goals_cols:
        conn.execute(
            text(
                "ALTER TABLE goals ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"
            )
        )
    if "priority" not in goals_cols:
        conn.execute(
            text(
                "ALTER TABLE goals ADD COLUMN priority VARCHAR(32) NOT NULL DEFAULT 'normal'"
            )
        )
    if "importance" not in goals_cols:
        conn.execute(
            text(
                "ALTER TABLE goals ADD COLUMN importance VARCHAR(32) NOT NULL DEFAULT 'normal'"
            )
        )
    if "source_inspiration_space_id" not in goals_cols:
        conn.execute(
            text("ALTER TABLE goals ADD COLUMN source_inspiration_space_id INTEGER")
        )
    if "source_inspiration_draft_id" not in goals_cols:
        conn.execute(
            text("ALTER TABLE goals ADD COLUMN source_inspiration_draft_id INTEGER")
        )

    task_cols = _table_columns(conn, "tasks")
    if "content" not in task_cols:
        conn.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN content VARCHAR(4000) NOT NULL DEFAULT ''"
            )
        )
        if "description" in task_cols:
            conn.execute(
                text(
                    "UPDATE tasks SET content = description "
                    "WHERE COALESCE(description, '') != ''"
                )
            )
    if "task_type" not in task_cols:
        conn.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN task_type VARCHAR(32) NOT NULL DEFAULT ''"
            )
        )
    if "estimated_minutes" not in task_cols:
        conn.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN estimated_minutes INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "context_key" not in task_cols:
        conn.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN context_key VARCHAR(256) NOT NULL DEFAULT ''"
            )
        )
    if "source_inspiration_space_id" not in task_cols:
        conn.execute(
            text("ALTER TABLE tasks ADD COLUMN source_inspiration_space_id INTEGER")
        )
    if "source_inspiration_draft_id" not in task_cols:
        conn.execute(
            text("ALTER TABLE tasks ADD COLUMN source_inspiration_draft_id INTEGER")
        )

    insp_space_cols = _table_columns(conn, "inspiration_spaces")
    if "mode" not in insp_space_cols:
        conn.execute(
            text(
                "ALTER TABLE inspiration_spaces ADD COLUMN mode VARCHAR(32) NOT NULL DEFAULT 'built_in'"
            )
        )
    if "workspace_path" not in insp_space_cols:
        conn.execute(
            text(
                "ALTER TABLE inspiration_spaces ADD COLUMN workspace_path VARCHAR(4000) NOT NULL DEFAULT ''"
            )
        )

    insp_res_cols = _table_columns(conn, "inspiration_resources")
    if "external_path" not in insp_res_cols:
        conn.execute(
            text(
                "ALTER TABLE inspiration_resources ADD COLUMN external_path VARCHAR(4000) NOT NULL DEFAULT ''"
            )
        )
    if "source" not in insp_res_cols:
        conn.execute(
            text(
                "ALTER TABLE inspiration_resources ADD COLUMN source VARCHAR(64) NOT NULL DEFAULT 'user'"
            )
        )

    space_cols = _table_columns(conn, "agent_spaces")
    if "companion_id" not in space_cols:
        conn.execute(text("ALTER TABLE agent_spaces ADD COLUMN companion_id INTEGER"))

    term_cols = _table_columns(conn, "remote_terminal_sessions")
    if "name" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN name VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
        term_cols.append("name")
    if "backend" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN backend VARCHAR(32) NOT NULL DEFAULT 'ttyd'"
            )
        )
    if "connect_url" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN connect_url VARCHAR(1024) NOT NULL DEFAULT ''"
            )
        )
