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
