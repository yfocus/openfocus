# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentEventIn(BaseModel):
    kind: str = Field(
        min_length=1, max_length=128, description="事件类型，如 progress/update"
    )
    agent: str = Field(
        min_length=1, max_length=256, description="上报方标识，如 coco/trae"
    )
    task_id: str | None = Field(default=None, max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict, description="原始上报内容")


class FocusReportIn(BaseModel):
    """外部 agent 通过 skill 上报任务完成情况。"""

    agent: str = Field(
        min_length=1, max_length=256, description="上报方标识，如 coco/trae/claude-code"
    )
    task_name: str = Field(min_length=1, max_length=512)
    status: str = Field(
        min_length=1,
        max_length=64,
        description="例如 running/succeeded/failed/canceled",
    )

    # 可选关联字段
    goal_id: int | None = None
    task_public_id: str | None = Field(default=None, max_length=36)

    user_prompt: str = Field(default="", max_length=20000)
    assistant_response: str = Field(default="", max_length=20000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSpaceCreateIn(BaseModel):
    companion_id: int = Field(description="Companion 环境 ID")
    root_path: str = Field(
        min_length=1, max_length=4000, description="本地工作目录（绝对路径）"
    )
