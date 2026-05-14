# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base


def test_migration_service_records_baseline_on_current_schema(tmp_path):
    from openfocus.infrastructure import migrations
    from openfocus.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'baseline.db'}", future=True)
    migrations.initialize_database(engine, Base)

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id FROM schema_migrations")).fetchall()
        ids = {str(row[0]) for row in rows}
        assert migrations.STARTUP_SCHEMA_MIGRATION_ID in ids


def test_alembic_upgrade_head_creates_current_schema(monkeypatch, tmp_path):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "alembic.db"
    monkeypatch.setenv("OPENFOCUS_DB_PATH", str(db_path))
    cfg = Config("alembic.ini")

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

    assert "goals" in tables
    assert "tasks" in tables
    assert "remote_terminal_sessions" in tables
    assert "companions" in tables
    assert version == "20260512_0001"


def test_migration_service_upgrades_minimal_legacy_tables(tmp_path):
    from openfocus.infrastructure import migrations

    LegacyBase = declarative_base()
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE goals (id INTEGER PRIMARY KEY, content VARCHAR(2000) NOT NULL DEFAULT '', description VARCHAR(4000) NOT NULL DEFAULT '')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL, description VARCHAR(4000) NOT NULL DEFAULT '')"
            )
        )
        conn.execute(text("CREATE TABLE inspiration_spaces (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text("CREATE TABLE inspiration_resources (id INTEGER PRIMARY KEY)")
        )
        conn.execute(text("CREATE TABLE agent_spaces (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE remote_terminal_sessions ("
                "id INTEGER PRIMARY KEY, "
                "space_id INTEGER NOT NULL DEFAULT 0, "
                "task_public_id VARCHAR(36) NOT NULL DEFAULT ''"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO remote_terminal_sessions "
                "(id, space_id, task_public_id) VALUES "
                "(1, 12, 'task-public-id'), (2, -34, '')"
            )
        )

    migrations.initialize_database(engine, LegacyBase)

    with engine.begin() as conn:
        goal_cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(goals)")}
        task_cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")}
        insp_space_cols = {
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(inspiration_spaces)")
        }
        insp_res_cols = {
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info(inspiration_resources)")
        }
        terminal_cols = {
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info(remote_terminal_sessions)")
        }
        migration_ids = {
            str(r[0]) for r in conn.execute(text("SELECT id FROM schema_migrations"))
        }
        terminal_rows = conn.execute(
            text(
                "SELECT id, owner_type, owner_id, task_public_id "
                "FROM remote_terminal_sessions ORDER BY id"
            )
        ).fetchall()
        task_public_id_col = next(
            r
            for r in conn.exec_driver_sql(
                "PRAGMA table_info(remote_terminal_sessions)"
            ).fetchall()
            if r[1] == "task_public_id"
        )
        conn.execute(
            text(
                "INSERT INTO remote_terminal_sessions "
                "(owner_type, owner_id, space_id, task_public_id, root_path, name, terminal_id, backend, connect_url, status) "
                "VALUES ('inspiration_space', 56, -56, NULL, '/tmp', 'terminal', 'terminal-null-task-id', 'ttyd', 'http://127.0.0.1', 'active')"
            )
        )

    assert {"title", "status", "priority", "importance"}.issubset(goal_cols)
    assert {"content", "task_type", "estimated_minutes", "context_key"}.issubset(
        task_cols
    )
    assert {"mode", "workspace_path"}.issubset(insp_space_cols)
    assert {"external_path", "source"}.issubset(insp_res_cols)
    assert {
        "owner_type",
        "owner_id",
        "space_id",
        "task_public_id",
        "companion_id",
        "root_path",
        "name",
        "terminal_id",
        "backend",
        "connect_url",
        "status",
        "created_at",
        "updated_at",
    }.issubset(terminal_cols)
    assert terminal_rows[0][1:] == ("agent_space", 12, "task-public-id")
    assert terminal_rows[1][1:] == ("inspiration_space", 34, None)
    assert task_public_id_col[3] == 0
    assert migrations.STARTUP_SCHEMA_MIGRATION_ID in migration_ids
    assert migrations.REMOTE_TERMINAL_OWNER_MIGRATION_ID in migration_ids
    assert (
        migrations.REMOTE_TERMINAL_TASK_PUBLIC_ID_NULLABLE_MIGRATION_ID in migration_ids
    )
