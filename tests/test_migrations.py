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
            text("CREATE TABLE remote_terminal_sessions (id INTEGER PRIMARY KEY)")
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

    assert {"title", "status", "priority", "importance"}.issubset(goal_cols)
    assert {"content", "task_type", "estimated_minutes", "context_key"}.issubset(
        task_cols
    )
    assert {"mode", "workspace_path"}.issubset(insp_space_cols)
    assert {"external_path", "source"}.issubset(insp_res_cols)
    assert {"name", "backend", "connect_url"}.issubset(terminal_cols)
    assert migrations.STARTUP_SCHEMA_MIGRATION_ID in migration_ids
