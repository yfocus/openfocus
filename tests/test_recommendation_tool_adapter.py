# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path

from openfocus.db import session_scope
from openfocus.domains.goals import service as goal_service
from openfocus.domains.recommendations.tool_adapter import (
    build_recommendation_tool_registry,
)
from openfocus.models import Event


def test_recommendation_tool_registry_exposes_memory_event_and_goal_tools(
    monkeypatch, tmp_path
):
    mem_dir = tmp_path / "memory"
    daily_dir = mem_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-23.md").write_text("Today focus", encoding="utf-8")
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(mem_dir))

    with session_scope() as s:
        goal_service.create_goal(
            s,
            title="Registry goal",
            content="Goal tools still compose into the recommendation registry",
            due_date=dt.date.today(),
            audit=False,
        )

    reg = build_recommendation_tool_registry()

    tool_names = {spec.name for spec in reg.specs()}
    assert {
        "list_daily_memory_files",
        "read_daily_memory_file",
        "list_recent_events",
        "list_goals",
        "describe_goal",
        "describe_gloal",
    }.issubset(tool_names)
    daily_files = json.loads(reg.call("list_daily_memory_files", {"limit": 10}))
    assert daily_files["files"][0]["rel_path"] == "daily/2026-05-23.md"
    goal_payload = json.loads(reg.call("list_goals", {"limit": 10}))
    assert any(g["title"] == "Registry goal" for g in goal_payload["goals"])


def test_recommendation_tool_registry_lists_recent_events_with_pagination():
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as s:
        s.add_all(
            [
                Event(
                    kind="task.progress",
                    agent="test",
                    task_id=f"task-{i}",
                    payload={"index": i},
                    created_at=now + dt.timedelta(seconds=i),
                )
                for i in range(3)
            ]
        )

    reg = build_recommendation_tool_registry()

    first_page = json.loads(reg.call("list_recent_events", {"limit": 2}))
    second_page = json.loads(reg.call("list_recent_events", {"limit": 2, "offset": 2}))

    assert first_page["limit"] == 2
    assert first_page["offset"] == 0
    assert first_page["next_offset"] == 2
    assert [ev["payload"]["index"] for ev in first_page["events"]] == [2, 1]
    assert [ev["payload"]["index"] for ev in second_page["events"]] == [0]
    assert second_page["next_offset"] == 3
    assert first_page["events"][0]["kind"] == "task.progress"
    assert first_page["events"][0]["agent"] == "test"
    assert first_page["events"][0]["task_id"] == "task-2"
    assert first_page["events"][0]["created_at"]


def test_read_daily_memory_file_rejects_non_daily_rel_path(monkeypatch, tmp_path):
    mem_dir = tmp_path / "memory"
    daily_dir = mem_dir / "daily"
    daily_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("Long-term memory", encoding="utf-8")
    (daily_dir / "2026-05-23.md").write_text("Daily memory", encoding="utf-8")
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(mem_dir))

    reg = build_recommendation_tool_registry()

    rejected = json.loads(reg.call("read_daily_memory_file", {"rel_path": "MEMORY.md"}))
    accepted = json.loads(
        reg.call("read_daily_memory_file", {"rel_path": "daily/2026-05-23.md"})
    )

    assert rejected == {
        "error": "not a daily memory file",
        "rel_path": "MEMORY.md",
    }
    assert accepted == {
        "rel_path": "daily/2026-05-23.md",
        "content": "Daily memory",
    }


def test_attention_scheduler_does_not_import_direct_db_or_event_model():
    source = Path("openfocus/agent/agents/attention_scheduler.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.endswith("db") and any(
                alias.name == "session_scope" for alias in node.names
            ):
                forbidden_imports.append("session_scope")
            if module.endswith("models") and any(
                alias.name == "Event" for alias in node.names
            ):
                forbidden_imports.append("Event")
        elif isinstance(node, ast.Import):
            forbidden_imports.extend(
                alias.name for alias in node.names if alias.name == "openfocus.db"
            )

    assert forbidden_imports == []
