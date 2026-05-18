# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    content: Mapped[str] = mapped_column(String(4000), nullable=False, default="")

    @property
    def description(self) -> str:
        """Backward-compatible alias for old rows/code; public model is title/content."""

        return self.content

    @description.setter
    def description(self, value: str) -> None:
        self.content = str(value or "")

    @property
    def summary(self) -> str:
        """Deprecated display summary; titles are truncated at render time instead."""

        return ""

    @summary.setter
    def summary(self, value: str) -> None:
        # Intentionally ignored: Goal only exposes title/content now.
        return None

    # 可先用枚举字符串（后续再做 Enum/字典表）
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    importance: Mapped[str] = mapped_column(
        String(32), nullable=False, default="normal"
    )

    due_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source_inspiration_space_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_inspiration_draft_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    agent: Mapped[str] = mapped_column(String(256), nullable=False)

    # 进度上报场景下通常会关联某个 task/run；先做可选字段，避免过早绑定概念。
    task_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 给外部 agent 用的稳定标识（避免泄漏自增 id）
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )

    goal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(String(4000), nullable=False, default="")

    @property
    def description(self) -> str:
        """Backward-compatible alias for old rows/code; public model is title/content."""

        return self.content

    @description.setter
    def description(self, value: str) -> None:
        self.content = str(value or "")

    @property
    def summary(self) -> str:
        """Deprecated display summary; titles are truncated at render time instead."""

        return ""

    @summary.setter
    def summary(self, value: str) -> None:
        # Intentionally ignored: Task only exposes title/content now.
        return None

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="todo")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_key: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    source_inspiration_space_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_inspiration_draft_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NextMoveRun(Base):
    __tablename__ = "next_move_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    trigger_kind: Mapped[str] = mapped_column(
        String(64), nullable=False, default="manual_refresh"
    )
    context_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    recommendations: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class NextMoveFeedback(Base):
    __tablename__ = "next_move_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    feedback_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="dismiss"
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason_text: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    learned_summary: Mapped[str] = mapped_column(
        String(2000), nullable=False, default=""
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class AttentionItem(Base):
    """Persistent, user-actionable notification derived from high-value events."""

    __tablename__ = "attention_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    task_public_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    goal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    summary: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InspirationSpace(Base):
    __tablename__ = "inspiration_spaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="built_in")
    workspace_path: Mapped[str] = mapped_column(
        String(4000), nullable=False, default=""
    )
    published_goal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forked_from_space_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    message_turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_phase_summary_turn: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_phase_summary_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InspirationMessage(Base):
    __tablename__ = "inspiration_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="message")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class InspirationResource(Base):
    __tablename__ = "inspiration_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_seq_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url_content: Mapped[str] = mapped_column(String(4000), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(4000), nullable=False, default="")
    external_path: Mapped[str] = mapped_column(String(4000), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="user")
    is_system_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class InspirationDraft(Base):
    __tablename__ = "inspiration_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_title: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    goal_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tasks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    open_questions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    rejected_or_deferred_ideas: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class InspirationPublishRecord(Base):
    __tablename__ = "inspiration_publish_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_goal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_task_ids: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=list
    )
    deferred_tasks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    summary_resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class AgentSpace(Base):
    __tablename__ = "agent_spaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 绑定 Task.public_id（对外稳定标识）；避免依赖自增 id
    task_public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)

    # 该 AgentSpace 运行在哪个 Companion 环境上（为空表示尚未绑定/未升级数据）。
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    root_path: Mapped[str] = mapped_column(String(4000), nullable=False)
    agent_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="trae-cli"
    )
    start_agent_command: Mapped[str] = mapped_column(
        String(2000), nullable=False, default=""
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AgentSpacePrompt(Base):
    __tablename__ = "agent_space_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AgentSession(Base):
    """AgentSpace 下的对话会话（OpenFocus 侧持久化）。

    说明：Companion 侧 session 可丢失，但 OpenFocus 侧对话应在服务重启后仍可回放。
    """

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 稳定会话 ID（用于 OpenFocus <-> Companion 对齐；避免泄漏自增 id）
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_path: Mapped[str] = mapped_column(String(4000), nullable=False)
    agent_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="trae-cli"
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )  # active/terminated
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AgentMessage(Base):
    """会话内消息。

    assistant 消息支持按 request_id 增量追加（用于 SSE chunk 持久化）。
    """

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 一次 agent 调用的 request_id（assistant chunk 用；user 消息为空）
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # user/assistant
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AgentRuntimeSession(Base):
    """Runtime session state derived from Companion/runtime signals."""

    __tablename__ = "agent_runtime_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    agent_runtime: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    task_public_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    workspace_root: Mapped[str] = mapped_column(
        String(4000), nullable=False, default=""
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    last_signal_kind: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class AgentTurn(Base):
    """Normalized agent runtime turn derived from Companion/runtime signals."""

    __tablename__ = "agent_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    agent_runtime: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    task_public_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_signal_kind: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )
    summary: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    error: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    state_started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class TaskAgentActivity(Base):
    """Current runtime activity read model for a task."""

    __tablename__ = "task_agent_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    active_turn_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    agent_runtime: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    summary: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    state_started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class RemoteTerminalSession(Base):
    """远程终端会话元信息（用于 tab 管理与 owner 生命周期清理）。"""

    __tablename__ = "remote_terminal_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="agent_space"
    )
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Legacy compatibility column: new code must query owner_type/owner_id instead.
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_public_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_path: Mapped[str] = mapped_column(String(4000), nullable=False)

    # 展示名（用于 UI tab）。要求：同一 space 下不重复。
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    terminal_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False, default="ttyd")
    connect_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )  # active/closed

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class RemoteTerminalOutput(Base):
    """远程终端输出日志（用于刷新/重进页面时回放）。

    说明：只持久化输出流（包含用户输入的回显 + 程序输出），避免重复记录 keypress。
    """

    __tablename__ = "remote_terminal_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # base64 编码后的 bytes（避免 SQLite BLOB / JSON 序列化差异）
    data_b64: Mapped[str] = mapped_column(Text, nullable=False, default="")
    nbytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class BrowserCompanionBinding(Base):
    __tablename__ = "browser_companion_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    browser_session_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    companion_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_method: Mapped[str] = mapped_column(
        String(64), nullable=False, default="nonce_protocol"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    last_verified_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class BrowserBindChallenge(Base):
    __tablename__ = "browser_bind_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    browser_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    companion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )


class Companion(Base):
    __tablename__ = "companions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Companion 侧稳定标识（本机持久化）。
    device_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # pending_certification | active
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="pending_certification"
    )

    # 配对完成后下发/保存的 token（OpenFocus -> Companion 反向代理用）
    auth_token: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 输入认证码限流（每分钟最多 3 次）
    pair_attempt_window_start: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pair_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
    )
