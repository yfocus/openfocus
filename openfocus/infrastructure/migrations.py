# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

STARTUP_SCHEMA_MIGRATION_ID = "20260511_startup_schema_baseline"
REMOTE_TERMINAL_OWNER_MIGRATION_ID = "20260512_remote_terminal_owner_fields"
REMOTE_TERMINAL_TASK_PUBLIC_ID_NULLABLE_MIGRATION_ID = (
    "20260514_remote_terminal_task_public_id_nullable"
)
ATTENTION_ITEMS_MIGRATION_ID = "20260514_attention_items"
AGENT_SPACE_START_COMMAND_MIGRATION_ID = "20260516_agent_space_start_command"
AGENT_SPACE_PROMPTS_MIGRATION_ID = "20260516_agent_space_prompts"


def _table_columns(conn: Any, table_name: str) -> list[str]:
    return [
        r[1]
        for r in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    ]


def _table_column(conn: Any, table_name: str, column_name: str) -> Any | None:
    for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall():
        if str(row[1]) == str(column_name):
            return row
    return None


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


def _is_applied(conn: Any, migration_id: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE id = :id"),
        {"id": str(migration_id)},
    ).one_or_none()
    return row is not None


def _apply_migration(conn: Any, migration_id: str, migrate_fn: Any) -> None:
    if _is_applied(conn, migration_id):
        return
    migrate_fn(conn)
    _mark_applied(conn, migration_id)


def initialize_database(engine: Engine, base: Any) -> None:
    """Create the current schema and run OpenFocus startup migrations.

    This is a deliberately small migration runner. It makes existing startup
    schema repair explicit and records a baseline id, while avoiding a partial
    Alembic integration until the project is ready to own full revision files.
    """

    base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _ensure_schema_migrations_table(conn)
        for migration_id, migrate_fn in STARTUP_MIGRATIONS:
            _apply_migration(conn, migration_id, migrate_fn)


def run_startup_schema_migrations(conn: Any) -> None:
    for _migration_id, migrate_fn in STARTUP_MIGRATIONS:
        migrate_fn(conn)


def _migrate_startup_schema_baseline(conn: Any) -> None:
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
    if "start_agent_command" not in space_cols:
        conn.execute(
            text(
                "ALTER TABLE agent_spaces ADD COLUMN start_agent_command "
                "VARCHAR(2000) NOT NULL DEFAULT ''"
            )
        )


def _migrate_remote_terminal_owner_fields(conn: Any) -> None:

    term_cols = _table_columns(conn, "remote_terminal_sessions")
    if "owner_type" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions "
                "ADD COLUMN owner_type VARCHAR(32) NOT NULL DEFAULT 'agent_space'"
            )
        )
        term_cols.append("owner_type")
    if "owner_id" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions "
                "ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0"
            )
        )
        term_cols.append("owner_id")
    if "space_id" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN space_id INTEGER NOT NULL DEFAULT 0"
            )
        )
        term_cols.append("space_id")
    if "task_public_id" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN task_public_id VARCHAR(36)"
            )
        )
        term_cols.append("task_public_id")
    if "companion_id" not in term_cols:
        conn.execute(
            text("ALTER TABLE remote_terminal_sessions ADD COLUMN companion_id INTEGER")
        )
        term_cols.append("companion_id")
    if "root_path" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions "
                "ADD COLUMN root_path VARCHAR(4000) NOT NULL DEFAULT ''"
            )
        )
        term_cols.append("root_path")
    if "name" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN name VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
        term_cols.append("name")
    if "terminal_id" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions "
                "ADD COLUMN terminal_id VARCHAR(64) NOT NULL DEFAULT ''"
            )
        )
        term_cols.append("terminal_id")
    if "backend" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN backend VARCHAR(32) NOT NULL DEFAULT 'ttyd'"
            )
        )
        term_cols.append("backend")
    if "connect_url" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions ADD COLUMN connect_url VARCHAR(1024) NOT NULL DEFAULT ''"
            )
        )
        term_cols.append("connect_url")
    if "status" not in term_cols:
        conn.execute(
            text(
                "ALTER TABLE remote_terminal_sessions "
                "ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"
            )
        )
        term_cols.append("status")
    if "created_at" not in term_cols:
        conn.execute(
            text("ALTER TABLE remote_terminal_sessions ADD COLUMN created_at DATETIME")
        )
        term_cols.append("created_at")
    if "updated_at" not in term_cols:
        conn.execute(
            text("ALTER TABLE remote_terminal_sessions ADD COLUMN updated_at DATETIME")
        )
        term_cols.append("updated_at")

    conn.execute(
        text(
            "UPDATE remote_terminal_sessions "
            "SET owner_type = CASE WHEN space_id < 0 THEN 'inspiration_space' ELSE 'agent_space' END "
            "WHERE owner_type IS NULL OR owner_type = '' OR owner_id = 0"
        )
    )
    conn.execute(
        text(
            "UPDATE remote_terminal_sessions "
            "SET owner_id = CASE WHEN space_id < 0 THEN -space_id ELSE space_id END "
            "WHERE owner_id IS NULL OR owner_id = 0"
        )
    )
    task_public_id_col = _table_column(
        conn, "remote_terminal_sessions", "task_public_id"
    )
    if task_public_id_col is not None and not bool(task_public_id_col[3]):
        conn.execute(
            text(
                "UPDATE remote_terminal_sessions "
                "SET task_public_id = NULL WHERE task_public_id = ''"
            )
        )


def _migrate_remote_terminal_task_public_id_nullable(conn: Any) -> None:
    """Migrate remote_terminal_sessions to the current schema."""

    term_cols = _table_columns(conn, "remote_terminal_sessions")
    if not term_cols:
        return

    task_public_id_col = _table_column(
        conn, "remote_terminal_sessions", "task_public_id"
    )
    if task_public_id_col is None:
        return
    if not bool(task_public_id_col[3]):
        conn.execute(
            text(
                "UPDATE remote_terminal_sessions "
                "SET task_public_id = NULL WHERE task_public_id = ''"
            )
        )
        return

    old_table = "remote_terminal_sessions__old"
    conn.execute(text(f"DROP TABLE IF EXISTS {old_table}"))
    conn.execute(text(f"ALTER TABLE remote_terminal_sessions RENAME TO {old_table}"))
    conn.execute(
        text(
            "CREATE TABLE remote_terminal_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "owner_type VARCHAR(32) NOT NULL, "
            "owner_id INTEGER NOT NULL, "
            "space_id INTEGER NOT NULL, "
            "task_public_id VARCHAR(36), "
            "companion_id INTEGER, "
            "root_path VARCHAR(4000) NOT NULL, "
            "name VARCHAR(128) NOT NULL, "
            "terminal_id VARCHAR(64) NOT NULL UNIQUE, "
            "backend VARCHAR(32) NOT NULL, "
            "connect_url VARCHAR(1024) NOT NULL, "
            "status VARCHAR(32) NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
    )
    conn.execute(
        text(
            "INSERT INTO remote_terminal_sessions ("
            "id, owner_type, owner_id, space_id, task_public_id, companion_id, "
            "root_path, name, terminal_id, backend, connect_url, status, "
            "created_at, updated_at"
            ") "
            "SELECT "
            "id, "
            "COALESCE(NULLIF(owner_type, ''), CASE WHEN space_id < 0 THEN 'inspiration_space' ELSE 'agent_space' END), "
            "CASE WHEN owner_id IS NULL OR owner_id = 0 THEN CASE WHEN space_id < 0 THEN -space_id ELSE space_id END ELSE owner_id END, "
            "COALESCE(space_id, 0), "
            "NULLIF(task_public_id, ''), "
            "companion_id, "
            "COALESCE(root_path, ''), "
            "COALESCE(name, ''), "
            "COALESCE(NULLIF(terminal_id, ''), 'migrated-terminal-' || id), "
            "COALESCE(NULLIF(backend, ''), 'ttyd'), "
            "COALESCE(connect_url, ''), "
            "COALESCE(NULLIF(status, ''), 'active'), "
            "COALESCE(created_at, CURRENT_TIMESTAMP), "
            "COALESCE(updated_at, CURRENT_TIMESTAMP) "
            f"FROM {old_table}"
        )
    )
    conn.execute(text(f"DROP TABLE {old_table}"))


def _migrate_attention_items(conn: Any) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS attention_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source_event_id INTEGER NOT NULL UNIQUE, "
            "task_public_id VARCHAR(36) NOT NULL DEFAULT '', "
            "goal_id INTEGER, "
            "item_type VARCHAR(64) NOT NULL, "
            "severity VARCHAR(32) NOT NULL DEFAULT 'info', "
            "title VARCHAR(512) NOT NULL DEFAULT '', "
            "summary VARCHAR(2000) NOT NULL DEFAULT '', "
            "status VARCHAR(32) NOT NULL DEFAULT 'active', "
            "payload JSON NOT NULL DEFAULT '{}', "
            "created_at DATETIME, "
            "dismissed_at DATETIME, "
            "acted_at DATETIME"
            ")"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_attention_items_status_created "
            "ON attention_items(status, created_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_attention_items_task "
            "ON attention_items(task_public_id)"
        )
    )


def _migrate_agent_space_start_command(conn: Any) -> None:
    space_cols = _table_columns(conn, "agent_spaces")
    if not space_cols:
        return
    if "companion_id" not in space_cols:
        conn.execute(text("ALTER TABLE agent_spaces ADD COLUMN companion_id INTEGER"))
        space_cols.append("companion_id")
    if "start_agent_command" not in space_cols:
        conn.execute(
            text(
                "ALTER TABLE agent_spaces ADD COLUMN start_agent_command "
                "VARCHAR(2000) NOT NULL DEFAULT ''"
            )
        )


def _migrate_agent_space_prompts(conn: Any) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS agent_space_prompts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title VARCHAR(160) NOT NULL DEFAULT '', "
            "content TEXT NOT NULL DEFAULT '', "
            "enabled BOOLEAN NOT NULL DEFAULT 1, "
            "created_at DATETIME, "
            "updated_at DATETIME"
            ")"
        )
    )


STARTUP_MIGRATIONS = [
    (STARTUP_SCHEMA_MIGRATION_ID, _migrate_startup_schema_baseline),
    (REMOTE_TERMINAL_OWNER_MIGRATION_ID, _migrate_remote_terminal_owner_fields),
    (
        REMOTE_TERMINAL_TASK_PUBLIC_ID_NULLABLE_MIGRATION_ID,
        _migrate_remote_terminal_task_public_id_nullable,
    ),
    (ATTENTION_ITEMS_MIGRATION_ID, _migrate_attention_items),
    (AGENT_SPACE_START_COMMAND_MIGRATION_ID, _migrate_agent_space_start_command),
    (AGENT_SPACE_PROMPTS_MIGRATION_ID, _migrate_agent_space_prompts),
]
