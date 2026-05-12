# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt


def test_inspiration_publishing_creates_goal_tasks_and_published_summary():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import publishing, resources
    from openfocus.models import (
        Goal,
        InspirationDraft,
        InspirationPublishRecord,
        InspirationResource,
        InspirationSpace,
        Task,
    )

    with session_scope() as s:
        space = InspirationSpace(title="Publish domain idea", status="open")
        s.add(space)
        s.flush()
        workspace = resources.workspace_path(space, int(space.id))
        space.workspace_path = str(workspace)
        draft = InspirationDraft(
            space_id=int(space.id),
            version=1,
            goal_title="Published goal",
            goal_description="Goal content",
            tasks=[{"title": "Task A", "description": "Task content"}],
            open_questions=["Question?"],
            rejected_or_deferred_ideas=["Later idea"],
        )
        s.add(draft)
        s.flush()
        space_id = int(space.id)
        draft_id = int(draft.id)

    prepared = publishing.prepare_publish(space_id, draft_id, dt.date(2026, 5, 12))
    assert prepared["previous_status"] == "open"

    audit_events: list[dict] = []
    publishing.publish_sync(
        space_id=space_id,
        draft_id=draft_id,
        due_date_iso="2026-05-12",
        previous_status="open",
        audit=lambda **kw: audit_events.append(kw),
    )

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "publishing_releasing"
        goal = s.query(Goal).one()
        task = s.query(Task).one()
        record = s.query(InspirationPublishRecord).one()
        summary = (
            s.query(InspirationResource)
            .filter(InspirationResource.name == "Published Summary")
            .one()
        )

        assert goal.title == "Published goal"
        assert task.title == "Task A"
        assert int(task.goal_id) == int(goal.id)
        assert int(record.created_goal_id) == int(goal.id)
        assert record.created_task_ids == [int(task.id)]
        assert summary.source == "system"
        assert summary.external_path.startswith("resources/")
        assert "Published tasks" in summary.text_content
        assert "Later idea" in summary.text_content

    assert audit_events[-1]["kind"] == "inspiration.published"
