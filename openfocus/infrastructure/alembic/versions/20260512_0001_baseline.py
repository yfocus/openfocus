# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260512_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("content", sa.String(length=4000), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        sa.Column(
            "priority", sa.String(length=32), nullable=False, server_default="normal"
        ),
        sa.Column(
            "importance", sa.String(length=32), nullable=False, server_default="normal"
        ),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("source_inspiration_space_id", sa.Integer(), nullable=True),
        sa.Column("source_inspiration_draft_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("agent", sa.String(length=256), nullable=False),
        sa.Column("task_id", sa.String(length=256), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("goal_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.String(length=4000), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="todo"
        ),
        sa.Column("task_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "estimated_minutes", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "context_key", sa.String(length=256), nullable=False, server_default=""
        ),
        sa.Column("source_inspiration_space_id", sa.Integer(), nullable=True),
        sa.Column("source_inspiration_draft_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "next_move_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "trigger_kind",
            sa.String(length=64),
            nullable=False,
            server_default="manual_refresh",
        ),
        sa.Column("context_summary", sa.JSON(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "next_move_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("task_public_id", sa.String(length=36), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_code", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column(
            "reason_text", sa.String(length=2000), nullable=False, server_default=""
        ),
        sa.Column(
            "learned_summary", sa.String(length=4000), nullable=False, server_default=""
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "attention_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_event_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "task_public_id", sa.String(length=36), nullable=False, server_default=""
        ),
        sa.Column("goal_id", sa.Integer(), nullable=True),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column(
            "severity", sa.String(length=32), nullable=False, server_default="info"
        ),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("summary", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_attention_items_status_created",
        "attention_items",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_attention_items_task",
        "attention_items",
        ["task_public_id"],
    )
    op.create_table(
        "inspiration_spaces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="open"
        ),
        sa.Column(
            "mode", sa.String(length=32), nullable=False, server_default="built_in"
        ),
        sa.Column(
            "workspace_path", sa.String(length=4000), nullable=False, server_default=""
        ),
        sa.Column("created_goal_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "inspiration_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "kind", sa.String(length=64), nullable=False, server_default="message"
        ),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "inspiration_resources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("resource_seq_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "url_content", sa.String(length=4000), nullable=False, server_default=""
        ),
        sa.Column(
            "file_path", sa.String(length=4000), nullable=False, server_default=""
        ),
        sa.Column(
            "external_path", sa.String(length=4000), nullable=False, server_default=""
        ),
        sa.Column(
            "source", sa.String(length=64), nullable=False, server_default="user"
        ),
        sa.Column(
            "is_system_generated", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "inspiration_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "goal_title", sa.String(length=2000), nullable=False, server_default=""
        ),
        sa.Column("goal_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("open_questions", sa.JSON(), nullable=False),
        sa.Column("rejected_or_deferred_ideas", sa.JSON(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "inspiration_publish_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("created_goal_id", sa.Integer(), nullable=False),
        sa.Column("created_task_ids", sa.JSON(), nullable=False),
        sa.Column("deferred_tasks", sa.JSON(), nullable=False),
        sa.Column("summary_resource_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_spaces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_public_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("companion_id", sa.Integer(), nullable=True),
        sa.Column("root_path", sa.String(length=4000), nullable=False),
        sa.Column(
            "agent_type",
            sa.String(length=64),
            nullable=False,
            server_default="trae-cli",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("task_public_id", sa.String(length=36), nullable=False),
        sa.Column("companion_id", sa.Integer(), nullable=True),
        sa.Column("root_path", sa.String(length=4000), nullable=False),
        sa.Column(
            "agent_type",
            sa.String(length=64),
            nullable=False,
            server_default="trae-cli",
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column(
            "request_id", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("done", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "remote_terminal_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_type",
            sa.String(length=32),
            nullable=False,
            server_default="agent_space",
        ),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("task_public_id", sa.String(length=36), nullable=True),
        sa.Column("companion_id", sa.Integer(), nullable=True),
        sa.Column("root_path", sa.String(length=4000), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("terminal_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "backend", sa.String(length=32), nullable=False, server_default="ttyd"
        ),
        sa.Column(
            "connect_url", sa.String(length=1024), nullable=False, server_default=""
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "remote_terminal_outputs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("terminal_id", sa.String(length=64), nullable=False),
        sa.Column("data_b64", sa.Text(), nullable=False, server_default=""),
        sa.Column("nbytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "companions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            sa.String(length=64),
            nullable=False,
            server_default="pending_certification",
        ),
        sa.Column(
            "auth_token", sa.String(length=256), nullable=False, server_default=""
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "pair_attempt_window_start", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "pair_attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for table_name in [
        "companions",
        "remote_terminal_outputs",
        "remote_terminal_sessions",
        "agent_messages",
        "agent_sessions",
        "agent_spaces",
        "inspiration_publish_records",
        "inspiration_drafts",
        "inspiration_resources",
        "inspiration_messages",
        "inspiration_spaces",
        "next_move_feedback",
        "attention_items",
        "next_move_runs",
        "tasks",
        "events",
        "goals",
    ]:
        op.drop_table(table_name)
