# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
from pathlib import Path


def infer_task_type(title: str, description: str) -> str:
    text = f"{title}\n{description}".lower()
    if any(
        k in text
        for k in [
            "review",
            "approve",
            "comment",
            "code review",
            "qa",
            "test report",
            "验收",
            "评审",
            "reviewer",
            " pr",
            " mr",
        ]
    ):
        return "review"
    if any(
        k in text
        for k in [
            "sync",
            "meeting",
            "reply",
            "email",
            "message",
            "call",
            "沟通",
            "对齐",
            "联系",
            "回复",
            "会议",
        ]
    ):
        return "communication"
    if any(
        k in text
        for k in [
            "admin",
            "ops",
            "cleanup",
            "organize",
            "docs",
            "document",
            "整理",
            "记录",
            "文档",
            "行政",
        ]
    ):
        return "admin"
    if any(
        k in text
        for k in [
            "design",
            "investigate",
            "analysis",
            "analyze",
            "refactor",
            "architecture",
            "research",
            "规划",
            "设计",
            "排查",
            "分析",
            "重构",
        ]
    ):
        return "deep_work"
    return "execution"


def infer_estimated_minutes(task_type: str, title: str, description: str) -> int:
    text = f"{title}\n{description}".lower()
    m = re.search(
        r"(\d{1,3})\s*(minutes?|mins?|min|小时|小時|hour|hours|hr|hrs|h|分钟|分鐘)",
        text,
    )
    if m:
        try:
            num = max(5, min(240, int(m.group(1))))
            unit = m.group(2)
            if unit in {"小时", "小時", "hour", "hours", "hr", "hrs", "h"}:
                return min(240, num * 60)
            return num
        except Exception:
            pass
    if re.search(
        r"\b(quick|small|tiny|minor|trivial|fast|马上|快速|小改|顺手)\b", text
    ):
        return 20
    if task_type == "review":
        return 25
    if task_type == "communication":
        return 20
    if task_type == "admin":
        return 15
    if task_type == "deep_work":
        return 90
    return 45


def infer_context_key(
    title: str, description: str, *, goal_id: int, root_path: str | None = None
) -> str:
    rp = str(root_path or "").strip()
    if rp:
        try:
            name = Path(rp).name.strip().lower()
            if name:
                return f"space:{name[:80]}"
        except Exception:
            pass
    text = f"{title}\n{description}".lower()
    m = re.search(r"([a-z0-9_.-]+/[a-z0-9_.-]+)", text)
    if m:
        return f"topic:{m.group(1)[:80]}"
    tokens = [
        x for x in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text) if len(x.strip()) >= 2
    ]
    seed = (tokens[0] if tokens else "")[:32].strip().lower()
    if seed:
        return f"goal:{goal_id}:{seed}"
    return f"goal:{goal_id}"
