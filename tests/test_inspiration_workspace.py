# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt


def test_inspiration_workspace_lists_and_loads_detail_payloads():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import (
        InspirationDraft,
        InspirationMessage,
        InspirationPublishRecord,
        InspirationResource,
        InspirationSpace,
    )

    older = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 5, 2, tzinfo=dt.timezone.utc)
    with session_scope() as s:
        first = InspirationSpace(
            title="First idea", status="open", last_activity_at=older
        )
        second = InspirationSpace(
            title="Second idea", status="closed", last_activity_at=newer
        )
        s.add_all([first, second])
        s.flush()
        first.workspace_path = str(resources.workspace_path(first, int(first.id)))
        second.workspace_path = str(resources.workspace_path(second, int(second.id)))
        s.add_all(
            [
                InspirationResource(
                    space_id=int(second.id),
                    resource_seq_id=1,
                    type="text",
                    name="Context",
                    text_content="Second context",
                ),
                InspirationDraft(
                    space_id=int(second.id),
                    version=1,
                    goal_title="Old draft",
                    goal_description="old",
                ),
                InspirationDraft(
                    space_id=int(second.id),
                    version=2,
                    goal_title="Latest draft",
                    goal_description="latest",
                ),
                InspirationPublishRecord(
                    space_id=int(second.id),
                    draft_id=22,
                    created_goal_id=33,
                    created_task_ids=[44],
                ),
                InspirationMessage(
                    space_id=int(second.id),
                    role="assistant",
                    kind="message",
                    content="hello",
                ),
            ]
        )
        second_id = int(second.id)

    listed = workspace.list_spaces(limit=50)
    assert listed["ok"] is True
    assert [item["title"] for item in listed["items"]][:2] == [
        "Second idea",
        "First idea",
    ]
    assert listed["items"][0]["resource_count"] == 1
    assert listed["items"][0]["draft_count"] == 2
    assert listed["items"][0]["publish_count"] == 1
    assert listed["items"][0]["latest_draft_version"] == 2

    detail = workspace.get_space_detail(second_id)
    assert detail["ok"] is True
    assert detail["item"]["id"] == second_id
    assert detail["item"]["latest_draft_version"] == 2
    assert detail["resources"][0]["text_content"] == "Second context"
    assert [draft["version"] for draft in detail["drafts"]] == [2, 1]
    assert detail["publish_records"][0]["created_task_ids"] == [44]
    assert detail["messages"][0]["content"] == "hello"


def test_inspiration_workspace_lists_drafts_by_latest_version_and_id():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import InspirationDraft, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Draft list", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        s.add_all(
            [
                InspirationDraft(
                    space_id=space_id,
                    version=1,
                    goal_title="v1",
                    goal_description="oldest",
                ),
                InspirationDraft(
                    space_id=space_id,
                    version=2,
                    goal_title="v2 older id",
                    goal_description="same version first",
                ),
                InspirationDraft(
                    space_id=space_id,
                    version=2,
                    goal_title="v2 newer id",
                    goal_description="same version second",
                ),
            ]
        )

    listed = workspace.list_drafts(space_id)

    assert listed["ok"] is True
    assert [item["goal_title"] for item in listed["items"]] == [
        "v2 newer id",
        "v2 older id",
        "v1",
    ]
    assert listed["items"][0]["version"] == 2


def test_inspiration_workspace_prepare_publish_marks_publishing_without_creating_goal_rows():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import Goal, InspirationDraft, InspirationSpace, Task

    with session_scope() as s:
        space = InspirationSpace(title="Publish prep", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        draft = InspirationDraft(
            space_id=space_id,
            version=1,
            goal_title="Prepared goal",
            goal_description="Goal body",
            tasks=[{"title": "Prepared task", "description": "Task body"}],
        )
        s.add(draft)
        s.flush()
        draft_id = int(draft.id)

    due_date = dt.date(2026, 6, 1)
    result = workspace.prepare_publish(space_id, draft_id, due_date)

    assert result.space_id == space_id
    assert result.draft_id == draft_id
    assert result.previous_status == "open"
    assert result.due_date == due_date.isoformat()
    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "publishing"
        assert s.query(Goal).count() == 0
        assert s.query(Task).count() == 0


def test_inspiration_workspace_prepare_publish_maps_conflict_and_unavailable_errors():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import InspirationDraft, InspirationMessage, InspirationSpace

    with session_scope() as s:
        waiting_space = InspirationSpace(title="Waiting", status="open")
        no_draft_space = InspirationSpace(title="No draft", status="open")
        empty_draft_space = InspirationSpace(title="Empty draft", status="open")
        s.add_all([waiting_space, no_draft_space, empty_draft_space])
        s.flush()
        waiting_id = int(waiting_space.id)
        no_draft_id = int(no_draft_space.id)
        empty_draft_id = int(empty_draft_space.id)
        s.add(
            InspirationMessage(
                space_id=waiting_id,
                role="assistant",
                kind="pending",
                content="thinking",
            )
        )
        s.add_all(
            [
                InspirationDraft(
                    space_id=waiting_id,
                    version=1,
                    goal_title="Waiting draft",
                    tasks=[{"title": "Task"}],
                ),
                InspirationDraft(
                    space_id=empty_draft_id,
                    version=1,
                    goal_title="Empty draft",
                    tasks=[],
                ),
            ]
        )

    due_date = dt.date(2026, 6, 1)
    try:
        workspace.prepare_publish(waiting_id, None, due_date)
    except workspace.InspirationWorkspaceConflict as exc:
        assert str(exc) == "Agent is still responding"
    else:
        raise AssertionError("pending message should block publish with conflict")

    for sid, detail in (
        (no_draft_id, "No draft is available for publishing"),
        (empty_draft_id, "The draft does not contain publishable tasks"),
    ):
        try:
            workspace.prepare_publish(sid, None, due_date)
        except workspace.InspirationWorkspaceValidationError as exc:
            assert str(exc) == detail
        else:
            raise AssertionError(f"space {sid} should not be publishable")

    try:
        workspace.prepare_publish(999999, None, due_date)
    except workspace.InspirationWorkspaceNotFound as exc:
        assert str(exc) == "Inspiration space not found"
    else:
        raise AssertionError("missing space should map to not found")


def test_inspiration_workspace_fork_space_clones_summary_and_selected_or_all_resources():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import (
        InspirationMessage,
        InspirationResource,
        InspirationSpace,
    )

    with session_scope() as s:
        source = InspirationSpace(title="Published idea", status="published")
        s.add(source)
        s.flush()
        source_id = int(source.id)
        source.workspace_path = str(resources.workspace_path(source, source_id))
        summary = InspirationResource(
            space_id=source_id,
            resource_seq_id=1,
            type="summary",
            name="Published Summary",
            text_content="Published summary body",
            source="system",
            is_system_generated=True,
        )
        selected = InspirationResource(
            space_id=source_id,
            resource_seq_id=2,
            type="text",
            name="Selected note",
            text_content="selected body",
        )
        omitted = InspirationResource(
            space_id=source_id,
            resource_seq_id=3,
            type="url",
            name="Omitted link",
            url_content="https://example.com/omitted",
        )
        s.add_all([summary, selected, omitted])
        s.flush()
        resources.write_resource_file(summary, source)
        resources.write_resource_file(selected, source)
        resources.write_resource_file(omitted, source)
        selected_id = int(selected.id)

    selected_fork = workspace.fork_space(
        source_id,
        title="",
        include_all_resources=False,
        resource_ids={selected_id},
    )

    assert selected_fork.item["title"] == "Published idea / Follow-up"
    assert selected_fork.item["forked_from_space_id"] == source_id
    assert selected_fork.audit_kind == "inspiration.forked"
    assert selected_fork.audit_metadata == {
        "space_id": source_id,
        "forked_space_id": int(selected_fork.item["id"]),
    }

    with session_scope() as s:
        forked_id = int(selected_fork.item["id"])
        forked = s.get(InspirationSpace, forked_id)
        assert forked is not None
        assert forked.status == "open"
        assert forked.forked_from_space_id == source_id
        cloned = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == forked_id)
            .order_by(InspirationResource.resource_seq_id.asc())
            .all()
        )
        assert {(res.type, res.name) for res in cloned} == {
            ("summary", "Published Summary"),
            ("text", "Selected note"),
        }
        assert sorted(res.resource_seq_id for res in cloned) == [1, 2]
        assert all(str(res.file_path or "").strip() for res in cloned)
        messages = s.query(InspirationMessage).filter_by(space_id=forked_id).all()
        assert len(messages) == 1
        assert messages[0].kind == "system"
        assert f"Forked from Inspiration space #{source_id}" in messages[0].content

    all_fork = workspace.fork_space(
        source_id,
        title="Custom fork",
        include_all_resources=True,
        resource_ids=set(),
    )

    with session_scope() as s:
        all_fork_id = int(all_fork.item["id"])
        cloned_names = {
            res.name
            for res in s.query(InspirationResource)
            .filter(InspirationResource.space_id == all_fork_id)
            .all()
        }
        assert cloned_names == {"Published Summary", "Selected note", "Omitted link"}
        assert all_fork.item["title"] == "Custom fork"


def test_inspiration_workspace_prepares_draft_generation_from_resource():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Resource draft", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        space.workspace_path = str(resources.workspace_path(space, space_id))
        active = InspirationResource(
            space_id=space_id,
            resource_seq_id=7,
            type="text",
            name="Bridge summary",
            text_content="# Goal\n\nBody\n\n## Task\n\nDo it",
        )
        deleted = InspirationResource(
            space_id=space_id,
            resource_seq_id=8,
            type="text",
            name="Deleted summary",
            text_content="ignore me",
            deleted_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add_all([active, deleted])
        s.flush()
        active_id = int(active.id)
        deleted_id = int(deleted.id)

    request = workspace.prepare_draft_from_resource(space_id, active_id)

    assert request.space_id == space_id
    assert request.prompt.startswith("/plan\n")
    assert "Create a Goal and Tasks using this resource as the primary source" in (
        request.prompt
    )
    assert "[Resource #7]" in request.prompt
    assert "Name: Bridge summary" in request.prompt
    assert "Type: text" in request.prompt

    for bad_id in (None, 0):
        try:
            workspace.prepare_draft_from_resource(space_id, bad_id)
        except workspace.InspirationWorkspaceValidationError as exc:
            assert str(exc) == "resource_id is required"
        else:
            raise AssertionError("missing resource_id should be rejected")

    for missing_id in (deleted_id, active_id + deleted_id + 1000):
        try:
            workspace.prepare_draft_from_resource(space_id, missing_id)
        except workspace.InspirationWorkspaceResourceNotFound as exc:
            assert str(exc) == "Resource not found"
        else:
            raise AssertionError("missing/deleted resources should be rejected")


def test_inspiration_workspace_prepares_draft_generation_from_draft_summary():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import Goal, InspirationResource, InspirationSpace, Task

    with session_scope() as s:
        space = InspirationSpace(title="Summary draft", status="open", mode="terminal")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)

    try:
        workspace.prepare_draft_from_draft_summary(space_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert str(exc) == "Summary is missing"
    else:
        raise AssertionError("missing summary should be rejected")

    summary_path = root / "resources" / "draft_summary.md"
    summary_path.write_text("# Goal\n\nBody\n\n## Task\n\nDo it\n", encoding="utf-8")

    request = workspace.prepare_draft_from_draft_summary(space_id)

    assert request.space_id == space_id
    assert request.prompt == "/plan"
    with session_scope() as s:
        assert s.query(Goal).count() == 0
        assert s.query(Task).count() == 0
        summary = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == space_id)
            .filter(InspirationResource.type == "summary")
            .one()
        )
        summary_id = int(summary.id)

    workspace.delete_resource(space_id, summary_id)
    summary_path.write_text("# Goal revived\n\nBody\n", encoding="utf-8")

    try:
        workspace.prepare_draft_from_draft_summary(space_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert str(exc) == "Summary is missing"
    else:
        raise AssertionError("tombstoned summary should not be resynced")

    with session_scope() as s:
        assert s.query(Goal).count() == 0
        assert s.query(Task).count() == 0
        active_summaries = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == space_id)
            .filter(InspirationResource.type == "summary")
            .filter(InspirationResource.deleted_at.is_(None))
            .all()
        )
        assert active_summaries == []


def test_inspiration_workspace_prepares_terminal_start_context():
    from pathlib import Path

    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationSpace

    with session_scope() as s:
        missing_path = InspirationSpace(title="Terminal start", status="open")
        preserved_path = InspirationSpace(
            title="Terminal existing path",
            status="open",
            workspace_path=str(resources.files_root() / "custom_terminal_workspace"),
        )
        closed = InspirationSpace(title="Closed terminal", status="closed")
        published = InspirationSpace(title="Published terminal", status="published")
        s.add_all([missing_path, preserved_path, closed, published])
        s.flush()
        missing_id = int(missing_path.id)
        preserved_id = int(preserved_path.id)
        preserved_root = resources.workspace_path(preserved_path, preserved_id)
        closed_id = int(closed.id)
        published_id = int(published.id)

    missing_prepared = workspace.prepare_terminal_start(missing_id)

    assert missing_prepared.space_id == missing_id
    assert missing_prepared.owner.owner_type == "inspiration_space"
    assert missing_prepared.owner.owner_id == missing_id
    assert missing_prepared.root_path == resources.workspace_path(None, missing_id)
    assert missing_prepared.base_path == (
        f"/api/inspirations/{missing_id}/terminals/{{terminal_id}}/ttyd/"
    )
    assert missing_prepared.audit_kind == "inspiration.terminal_created"
    assert missing_prepared.audit_metadata == {"space_id": missing_id}
    with session_scope() as s:
        space = s.get(InspirationSpace, missing_id)
        assert space is not None
        assert space.mode == "terminal"
        assert Path(space.workspace_path) == missing_prepared.root_path

    preserved_prepared = workspace.prepare_terminal_start(preserved_id)

    assert preserved_prepared.root_path == preserved_root
    with session_scope() as s:
        space = s.get(InspirationSpace, preserved_id)
        assert space is not None
        assert Path(space.workspace_path) == preserved_root
        assert space.mode == "terminal"

    for blocked_id in (closed_id, published_id):
        try:
            workspace.prepare_terminal_start(blocked_id)
        except workspace.InspirationWorkspaceValidationError as exc:
            assert str(exc) == "Only open spaces can start terminals"
        else:
            raise AssertionError("non-open spaces should not start terminals")


def test_inspiration_workspace_prepares_terminal_prompt_without_runtime():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Prompt bridge", status="open", mode="terminal")
        s.add(space)
        s.flush()
        space_id = int(space.id)

    prepared = workspace.prepare_terminal_prompt(space_id)

    assert prepared.space_id == space_id
    assert prepared.owner.owner_type == "inspiration_space"
    assert prepared.owner.owner_id == space_id
    assert "resources/draft_summary.md" in prepared.prompt
    assert "Prompt bridge" in prepared.prompt


def test_inspiration_workspace_close_reopen_and_delete_lifecycle():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import (
        InspirationDraft,
        InspirationMessage,
        InspirationResource,
        InspirationSpace,
    )

    with session_scope() as s:
        space = InspirationSpace(title="Lifecycle idea", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        space.workspace_path = str(resources.workspace_path(space, space_id))
        s.add_all(
            [
                InspirationMessage(
                    space_id=space_id,
                    role="assistant",
                    kind="draft_generated",
                    content="draft card",
                ),
                InspirationMessage(
                    space_id=space_id,
                    role="user",
                    kind="message",
                    content="keep me",
                ),
                InspirationDraft(
                    space_id=space_id,
                    version=1,
                    goal_title="Draft",
                    goal_description="Draft body",
                ),
            ]
        )

    closed = workspace.close_space(space_id)
    assert closed.item["status"] == "closed"
    assert closed.release_terminals is True
    assert closed.audit_kind == "inspiration.closed"

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "closed"
        assert space.closed_at is not None
        assert s.query(InspirationDraft).filter_by(space_id=space_id).count() == 0
        messages = (
            s.query(InspirationMessage)
            .filter_by(space_id=space_id)
            .order_by(InspirationMessage.id.asc())
            .all()
        )
        assert [msg.kind for msg in messages] == ["message", "system"]
        assert messages[0].content == "keep me"

    reopened = workspace.reopen_space(space_id)
    assert reopened.item["status"] == "open"
    assert reopened.release_terminals is False

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "open"
        resource = InspirationResource(
            space_id=space_id,
            resource_seq_id=1,
            type="text",
            name="Delete me",
            text_content="delete me",
        )
        s.add(resource)
        s.flush()
        resources.write_resource_file(resource, space)
        workspace_dir = resources.workspace_path(space, space_id)
        assert workspace_dir.exists()

    deleted = workspace.delete_space(space_id)
    assert deleted.space_id == space_id
    assert deleted.audit_kind == "inspiration.deleted"

    with session_scope() as s:
        assert s.get(InspirationSpace, space_id) is None
        assert s.query(InspirationMessage).filter_by(space_id=space_id).count() == 0
        assert s.query(InspirationResource).filter_by(space_id=space_id).count() == 0
    assert not workspace_dir.exists()


def test_inspiration_workspace_delete_skips_unsafe_persisted_workspace_path(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import (
        InspirationDraft,
        InspirationMessage,
        InspirationResource,
        InspirationSpace,
    )

    external_dir = tmp_path / "outside-inspiration-root"
    external_dir.mkdir()
    marker = external_dir / "keep.txt"
    marker.write_text("do not remove", encoding="utf-8")

    with session_scope() as s:
        space = InspirationSpace(
            title="Unsafe delete path", status="open", workspace_path=str(external_dir)
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        s.add_all(
            [
                InspirationMessage(
                    space_id=space_id,
                    role="user",
                    kind="message",
                    content="cleanup rows only",
                ),
                InspirationDraft(
                    space_id=space_id,
                    version=1,
                    goal_title="Draft",
                    goal_description="Draft body",
                ),
                InspirationResource(
                    space_id=space_id,
                    resource_seq_id=1,
                    type="text",
                    name="Resource",
                    text_content="resource body",
                ),
            ]
        )

    deleted = workspace.delete_space(space_id)

    assert deleted.space_id == space_id
    assert deleted.audit_kind == "inspiration.deleted"
    with session_scope() as s:
        assert s.get(InspirationSpace, space_id) is None
        assert s.query(InspirationMessage).filter_by(space_id=space_id).count() == 0
        assert s.query(InspirationDraft).filter_by(space_id=space_id).count() == 0
        assert s.query(InspirationResource).filter_by(space_id=space_id).count() == 0
    assert external_dir.exists()
    assert marker.read_text(encoding="utf-8") == "do not remove"


def test_inspiration_workspace_delete_skips_symlinked_default_workspace(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import (
        InspirationDraft,
        InspirationMessage,
        InspirationResource,
        InspirationSpace,
    )

    external_dir = tmp_path / "outside-linked-workspace"
    external_dir.mkdir()
    marker = external_dir / "keep.txt"
    marker.write_text("do not remove", encoding="utf-8")

    with session_scope() as s:
        space = InspirationSpace(
            title="Symlinked default workspace",
            status="open",
            workspace_path=str(external_dir),
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        default_link = resources.files_root() / f"space_{space_id}"
        default_link.symlink_to(external_dir, target_is_directory=True)
        s.add_all(
            [
                InspirationMessage(
                    space_id=space_id,
                    role="user",
                    kind="message",
                    content="cleanup rows only",
                ),
                InspirationDraft(
                    space_id=space_id,
                    version=1,
                    goal_title="Draft",
                    goal_description="Draft body",
                ),
                InspirationResource(
                    space_id=space_id,
                    resource_seq_id=1,
                    type="text",
                    name="Resource",
                    text_content="resource body",
                ),
            ]
        )

    deleted = workspace.delete_space(space_id)

    assert deleted.space_id == space_id
    assert deleted.audit_kind == "inspiration.deleted"
    with session_scope() as s:
        assert s.get(InspirationSpace, space_id) is None
        assert s.query(InspirationMessage).filter_by(space_id=space_id).count() == 0
        assert s.query(InspirationDraft).filter_by(space_id=space_id).count() == 0
        assert s.query(InspirationResource).filter_by(space_id=space_id).count() == 0
    assert external_dir.exists()
    assert marker.read_text(encoding="utf-8") == "do not remove"
    assert default_link.is_symlink()


def test_inspiration_workspace_rejects_invalid_lifecycle_transitions():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import workspace
    from openfocus.models import InspirationSpace

    with session_scope() as s:
        open_space = InspirationSpace(title="Open", status="open")
        published_space = InspirationSpace(title="Published", status="published")
        s.add_all([open_space, published_space])
        s.flush()
        open_id = int(open_space.id)
        published_id = int(published_space.id)

    try:
        workspace.reopen_space(open_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert "Only closed spaces can be reopened" in str(exc)
    else:
        raise AssertionError("open spaces should not reopen")

    try:
        workspace.close_space(published_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert "Published spaces cannot be closed" in str(exc)
    else:
        raise AssertionError("published spaces should not close")

    try:
        workspace.delete_space(published_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert "Published spaces cannot be deleted" in str(exc)
    else:
        raise AssertionError("published spaces should not delete")


def test_inspiration_workspace_resource_use_cases_manage_open_spaces_only():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    with session_scope() as s:
        open_space = InspirationSpace(title="Open resources", status="open")
        closed_space = InspirationSpace(title="Closed resources", status="closed")
        published_space = InspirationSpace(
            title="Published resources", status="published"
        )
        s.add_all([open_space, closed_space, published_space])
        s.flush()
        open_id = int(open_space.id)
        closed_id = int(closed_space.id)
        published_id = int(published_space.id)
        open_space.workspace_path = str(resources.workspace_path(open_space, open_id))
        closed_space.workspace_path = str(
            resources.workspace_path(closed_space, closed_id)
        )
        published_space.workspace_path = str(
            resources.workspace_path(published_space, published_id)
        )

    created = workspace.create_resource(
        open_id,
        resource_type="text",
        name="Draft note",
        text_content="original body",
    )
    resource_id = int(created.resource_id)
    assert created.item["name"] == "Draft note"
    assert created.item["text_content"] == "original body"
    assert created.audit_kind == "inspiration.resource_added"

    updated = workspace.update_resource(
        open_id,
        resource_id,
        {"name": "Draft note v2", "text_content": "updated body"},
    )
    assert updated.item["name"] == "Draft note v2"
    assert updated.item["text_content"] == "updated body"

    detail_before_delete = workspace.get_space_detail(open_id)
    assert [item["id"] for item in detail_before_delete["resources"]] == [resource_id]

    from pathlib import Path

    with session_scope() as s:
        resource_row = s.get(InspirationResource, resource_id)
        assert resource_row is not None
        resource_file = Path(str(resource_row.file_path))
        assert resource_file.exists()

    deleted = workspace.delete_resource(open_id, resource_id)
    assert deleted.resource_id == resource_id
    assert not resource_file.exists()
    detail_after_delete = workspace.get_space_detail(open_id)
    assert detail_after_delete["resources"] == []

    resynced = workspace.sync_resources(open_id)
    assert resource_file.name not in {
        Path(str(item.get("external_path") or "")).name for item in resynced.items
    }
    detail_after_sync = workspace.get_space_detail(open_id)
    assert detail_after_sync["resources"] == []

    for blocked_id in (closed_id, published_id):
        try:
            workspace.create_resource(
                blocked_id, resource_type="text", text_content="blocked"
            )
        except workspace.InspirationWorkspaceValidationError as exc:
            assert "Only open spaces accept new resources" in str(exc)
        else:
            raise AssertionError("non-open spaces should not create resources")

        try:
            workspace.update_resource(blocked_id, resource_id, {"name": "blocked"})
        except workspace.InspirationWorkspaceValidationError as exc:
            assert "Only open spaces can edit resources" in str(exc)
        else:
            raise AssertionError("non-open spaces should not edit resources")

        try:
            workspace.delete_resource(blocked_id, resource_id)
        except workspace.InspirationWorkspaceValidationError as exc:
            assert "Only open spaces can delete resources" in str(exc)
        else:
            raise AssertionError("non-open spaces should not delete resources")


def test_inspiration_workspace_resource_raw_file_contract():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="Raw resource", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        space.workspace_path = str(resources.workspace_path(space, space_id))

    slot = workspace.prepare_resource_upload(space_id)
    target_path, uploaded_name = resources.store_uploaded_resource_bytes(
        space_id=space_id,
        seq_id=slot.seq_id,
        original_name="diagram.png",
        content=b"image-bytes",
    )
    created = workspace.create_resource(
        space_id,
        resource_type="image",
        name="Diagram",
        stored_file=workspace.StoredResourceFile(
            seq_id=slot.seq_id, path=target_path, uploaded_name=uploaded_name
        ),
    )
    resource_id = int(created.resource_id)

    raw = workspace.raw_resource_file(space_id, resource_id)
    assert raw.path == target_path
    assert raw.media_type == "image/png"
    assert raw.filename == "Diagram"

    target_path.unlink()
    try:
        workspace.raw_resource_file(space_id, resource_id)
    except workspace.InspirationWorkspaceResourceNotFound as exc:
        assert "File resource not found" in str(exc)
    else:
        raise AssertionError("missing resource file should return a raw-file miss")

    try:
        workspace.raw_resource_file(space_id, resource_id + 1000)
    except workspace.InspirationWorkspaceResourceNotFound as exc:
        assert "Resource not found" in str(exc)
    else:
        raise AssertionError("missing resource row should return a resource miss")


def test_inspiration_workspace_sync_and_raw_reject_symlink_and_escaped_files(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    external_file = tmp_path / "outside-secret.txt"
    external_file.write_text("secret outside workspace", encoding="utf-8")

    with session_scope() as s:
        space = InspirationSpace(title="Unsafe resources", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)

    symlink_path = root / "resources" / "outside-link.md"
    symlink_path.symlink_to(external_file)

    synced = workspace.sync_resources(space_id)
    assert "resources/outside-link.md" not in {
        item.get("external_path") for item in synced.items
    }

    detail = workspace.get_space_detail(space_id)
    assert "resources/outside-link.md" not in {
        item.get("external_path") for item in detail["resources"]
    }

    with session_scope() as s:
        symlink_row = InspirationResource(
            space_id=space_id,
            resource_seq_id=1,
            type="text",
            name="Symlink row",
            text_content="",
            file_path=str(symlink_path),
            external_path="resources/outside-link.md",
        )
        escaped_row = InspirationResource(
            space_id=space_id,
            resource_seq_id=2,
            type="text",
            name="Escaped row",
            text_content="",
            file_path=str(external_file),
            external_path="resources/manual-escaped.md",
        )
        s.add_all([symlink_row, escaped_row])
        s.flush()
        raw_blocked_ids = [int(symlink_row.id), int(escaped_row.id)]

    for raw_blocked_id in raw_blocked_ids:
        try:
            workspace.raw_resource_file(space_id, raw_blocked_id)
        except workspace.InspirationWorkspaceResourceNotFound as exc:
            assert "File resource not found" in str(exc)
        else:
            raise AssertionError("unsafe resource file should not be served")


def test_inspiration_workspace_resource_sync_imports_terminal_files_and_blocks_published():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(
            title="Terminal sync", status="closed", mode="terminal"
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)

    draft_summary = root / "resources" / "draft_summary.md"
    draft_summary.write_text(
        "# Draft goal\n\nGoal body\n\n## Task one\n\nTask body\n", encoding="utf-8"
    )
    external_note = root / "resources" / "external_note.md"
    external_note.write_text("# External note\n\nCreated externally", encoding="utf-8")

    synced = workspace.sync_resources(space_id)
    assert synced.synced is True
    assert synced.item is not None
    assert synced.item["name"] == "Summary"
    assert synced.item["external_path"] == "resources/draft_summary.md"
    assert synced.audit_kind == "inspiration.resources_synced"
    assert {item["external_path"] for item in synced.items} >= {
        "resources/draft_summary.md",
        "resources/external_note.md",
    }

    detail = workspace.get_space_detail(space_id)
    assert {item["external_path"] for item in detail["resources"]} >= {
        "resources/draft_summary.md",
        "resources/external_note.md",
    }

    with session_scope() as s:
        published = s.get(InspirationSpace, space_id)
        assert published is not None
        published.status = "published"

    try:
        workspace.sync_resources(space_id)
    except workspace.InspirationWorkspaceValidationError as exc:
        assert "Published spaces are read-only" in str(exc)
    else:
        raise AssertionError("published spaces should not sync resources")


def test_inspiration_workspace_create_resource_rejects_symlinked_resources_root(
    tmp_path,
):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    external_dir = tmp_path / "outside-resources"
    external_dir.mkdir()

    with session_scope() as s:
        space = InspirationSpace(title="Symlink create", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)
        (root / "resources").rmdir()
        (root / "resources").symlink_to(external_dir, target_is_directory=True)

    try:
        workspace.create_resource(
            space_id,
            resource_type="text",
            name="Marker",
            text_content="must not leave workspace",
        )
    except workspace.InspirationWorkspaceValidationError as exc:
        assert "unsafe resources directory" in str(exc)
    else:
        raise AssertionError("symlinked resources root should reject text create")

    assert list(external_dir.iterdir()) == []
    with session_scope() as s:
        assert (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == space_id)
            .count()
            == 0
        )


def test_inspiration_workspace_replace_cleanup_skips_symlink_and_escaped_old_paths(
    tmp_path,
):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    external_file = tmp_path / "outside-old.png"
    external_file.write_bytes(b"outside-old")
    escaped_file = tmp_path / "escaped-old.png"
    escaped_file.write_bytes(b"escaped-old")

    with session_scope() as s:
        space = InspirationSpace(title="Replace cleanup", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)

        symlink_path = root / "resources" / "old-link.png"
        symlink_path.symlink_to(external_file)
        next_file = root / "resources" / "resource_1.png"
        next_file.write_bytes(b"new-one")
        symlink_row = InspirationResource(
            space_id=space_id,
            resource_seq_id=1,
            type="image",
            name="Linked old",
            file_path=str(symlink_path),
            external_path="resources/old-link.png",
        )

        escaped_next_file = root / "resources" / "resource_2.png"
        escaped_next_file.write_bytes(b"new-two")
        escaped_row = InspirationResource(
            space_id=space_id,
            resource_seq_id=2,
            type="image",
            name="Escaped old",
            file_path=str(escaped_file),
            external_path="resources/escaped-old.png",
        )
        s.add_all([symlink_row, escaped_row])
        s.flush()
        symlink_row_id = int(symlink_row.id)
        escaped_row_id = int(escaped_row.id)

    replaced_symlink = workspace.replace_resource_file(
        space_id,
        symlink_row_id,
        stored_file=workspace.StoredResourceFile(
            seq_id=1, path=next_file, uploaded_name="resource_1.png"
        ),
        name="Linked replaced",
    )
    assert replaced_symlink.item["name"] == "Linked replaced"
    assert symlink_path.is_symlink()
    assert external_file.read_bytes() == b"outside-old"

    replaced_escaped = workspace.replace_resource_file(
        space_id,
        escaped_row_id,
        stored_file=workspace.StoredResourceFile(
            seq_id=2, path=escaped_next_file, uploaded_name="resource_2.png"
        ),
        name="Escaped replaced",
    )
    assert replaced_escaped.item["name"] == "Escaped replaced"
    assert escaped_file.read_bytes() == b"escaped-old"


def test_inspiration_workspace_import_does_not_load_runtime_adapters(monkeypatch):
    import builtins
    import importlib
    import sys

    blocked = {
        "openfocus.domains.terminals.gateway",
        "openfocus.domains.companion.service",
    }
    inspirations_pkg = importlib.import_module("openfocus.domains.inspirations")
    for module_name in (
        "openfocus.domains.inspirations.workspace",
        "openfocus.domains.inspirations.terminal_bridge",
        "openfocus.domains.inspirations.service",
    ):
        sys.modules.pop(module_name, None)
    for attr in ("workspace", "terminal_bridge", "service"):
        if hasattr(inspirations_pkg, attr):
            delattr(inspirations_pkg, attr)

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        requested = {str(name)}
        requested.update(f"{name}.{item}" for item in (fromlist or ()))
        if blocked.intersection(requested):
            blocked_name = sorted(blocked.intersection(requested))[0]
            raise AssertionError(f"runtime adapter import blocked: {blocked_name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    workspace_mod = importlib.import_module("openfocus.domains.inspirations.workspace")
    terminal_bridge_mod = importlib.import_module(
        "openfocus.domains.inspirations.terminal_bridge"
    )

    assert workspace_mod.__name__.endswith(".workspace")
    assert terminal_bridge_mod.__name__.endswith(".terminal_bridge")
