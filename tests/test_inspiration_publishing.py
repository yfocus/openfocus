# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt

import pytest


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


def test_inspiration_publishing_records_unselected_tasks_as_deferred():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import publishing, resources
    from openfocus.models import (
        InspirationDraft,
        InspirationPublishRecord,
        InspirationResource,
        InspirationSpace,
        Task,
    )

    with session_scope() as s:
        space = InspirationSpace(title="Selective publish idea", status="open")
        s.add(space)
        s.flush()
        space.workspace_path = str(resources.workspace_path(space, int(space.id)))
        draft = InspirationDraft(
            space_id=int(space.id),
            version=1,
            goal_title="Selective publish goal",
            goal_description="Goal content",
            tasks=[
                {"title": "Publish Task A", "description": "Task A content"},
                {"title": "Defer Task B", "description": "Task B content"},
                {"title": "Publish Task C", "content": "Task C content"},
            ],
            open_questions=[],
            rejected_or_deferred_ideas=[],
        )
        s.add(draft)
        s.flush()
        space_id = int(space.id)
        draft_id = int(draft.id)

    prepared = publishing.prepare_publish(
        space_id,
        draft_id,
        dt.date(2026, 5, 12),
        selected_task_indexes=[2, 0, 2],
    )
    assert prepared["selected_task_indexes"] == [0, 2]

    publishing.publish_sync(
        space_id=space_id,
        draft_id=draft_id,
        due_date_iso="2026-05-12",
        previous_status="open",
        selected_task_indexes=prepared["selected_task_indexes"],
    )

    with session_scope() as s:
        tasks = s.query(Task).order_by(Task.id.asc()).all()
        assert [task.title for task in tasks] == ["Publish Task A", "Publish Task C"]
        assert [task.content for task in tasks] == ["Task A content", "Task C content"]

        record = s.query(InspirationPublishRecord).one()
        assert record.deferred_tasks == [
            {"title": "Defer Task B", "description": "Task B content"}
        ]

        summary = (
            s.query(InspirationResource)
            .filter(InspirationResource.name == "Published Summary")
            .one()
        )
        assert "- Publish Task A" in summary.text_content
        assert "- Publish Task C" in summary.text_content
        assert "- Defer Task B" in summary.text_content


@pytest.mark.anyio
async def test_kickoff_publish_records_release_failure_without_stuck_state(
    monkeypatch,
):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import publishing, resources
    from openfocus.domains.inspirations import service as inspiration_service
    from openfocus.models import InspirationDraft, InspirationMessage, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Release failure idea", status="open")
        s.add(space)
        s.flush()
        workspace = resources.workspace_path(space, int(space.id))
        space.workspace_path = str(workspace)
        draft = InspirationDraft(
            space_id=int(space.id),
            version=1,
            goal_title="Release failure goal",
            tasks=[{"title": "Release failure task"}],
        )
        s.add(draft)
        s.flush()
        space_id = int(space.id)
        draft_id = int(draft.id)

    prepared = publishing.prepare_publish(space_id, draft_id, dt.date(2026, 5, 12))
    audit_events: list[dict] = []
    monkeypatch.setattr(
        inspiration_service.memory_service,
        "try_audit_memory",
        lambda **kw: audit_events.append(kw),
    )

    async def failing_release(release_space_id: int) -> int:
        assert release_space_id == space_id
        raise RuntimeError("terminal release offline")

    await inspiration_service.kickoff_publish(
        space_id=space_id,
        draft_id=draft_id,
        due_date_iso=str(prepared["due_date"]),
        previous_status=str(prepared["previous_status"]),
        release_terminals=failing_release,
    )

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "published"
        message = (
            s.query(InspirationMessage)
            .filter(InspirationMessage.space_id == space_id)
            .order_by(InspirationMessage.id.desc())
            .first()
        )
        assert message is not None
        assert message.kind == "error"
        assert "failed to release inspiration terminals" in message.content
        assert message.payload["phase"] == "release_terminals"

    assert audit_events[-1]["kind"] == "inspiration.publish_release_error"


@pytest.mark.anyio
async def test_kickoff_publish_records_publish_exception_and_skips_release(
    monkeypatch,
):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import publishing, resources
    from openfocus.domains.inspirations import service as inspiration_service
    from openfocus.models import InspirationDraft, InspirationMessage, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Publish exception idea", status="open")
        s.add(space)
        s.flush()
        workspace = resources.workspace_path(space, int(space.id))
        space.workspace_path = str(workspace)
        draft = InspirationDraft(
            space_id=int(space.id),
            version=1,
            goal_title="Publish exception goal",
            tasks=[{"title": "Publish exception task"}],
        )
        s.add(draft)
        s.flush()
        space_id = int(space.id)
        draft_id = int(draft.id)

    prepared = publishing.prepare_publish(space_id, draft_id, dt.date(2026, 5, 12))
    audit_events: list[dict] = []
    release_calls: list[int] = []

    def failing_publish_sync(**_kwargs) -> None:
        raise RuntimeError("publish worker crashed")

    monkeypatch.setattr(inspiration_service, "publish_sync", failing_publish_sync)
    monkeypatch.setattr(
        inspiration_service.memory_service,
        "try_audit_memory",
        lambda **kw: audit_events.append(kw),
    )

    async def release_terminals(release_space_id: int) -> int:
        release_calls.append(release_space_id)
        return 0

    await inspiration_service.kickoff_publish(
        space_id=space_id,
        draft_id=draft_id,
        due_date_iso=str(prepared["due_date"]),
        previous_status=str(prepared["previous_status"]),
        release_terminals=release_terminals,
    )

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "open"
        message = (
            s.query(InspirationMessage)
            .filter(InspirationMessage.space_id == space_id)
            .order_by(InspirationMessage.id.desc())
            .first()
        )
        assert message is not None
        assert message.kind == "error"
        assert "Failed to publish the draft: publish worker crashed" in message.content

    assert release_calls == []
    assert audit_events[-1]["kind"] == "inspiration.publish_error"
