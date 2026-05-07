from __future__ import annotations

import datetime as dt

from openfocus.agent.tools.goals import build_goal_tools
from openfocus.db import session_scope
from openfocus.models import Goal, Task


def test_goal_tools_list_and_describe():
    with session_scope() as s:
        g = Goal(
            content="g",
            description="d",
            due_date=dt.date.today(),
            status="active",
            priority="urgent",
            importance="very_important",
        )
        s.add(g)
        s.flush()
        s.add(Task(goal_id=g.id, title="t1", status="todo"))
        s.add(Task(goal_id=g.id, title="t2", status="done"))
        goal_id = g.id

    reg = build_goal_tools()
    out = reg.call(
        "list_goals", {"only_unfinished": True, "priority": "urgent", "limit": 10}
    )
    assert '"goal_id":' in out
    detail = reg.call("describe_goal", {"goal_id": goal_id, "include_tasks": True})
    assert '"tasks":' in detail
    alias = reg.call("describe_gloal", {"goal_id": goal_id, "include_tasks": False})
    assert '"goal":' in alias
