# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations


def test_inspiration_resources_create_workspace_note_and_sync_summary():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources
    from openfocus.models import InspirationResource, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(title="BYO agent idea", mode="terminal")
        s.add(space)
        s.flush()

        workspace = resources.workspace_path(space, int(space.id))
        space.workspace_path = str(workspace)
        first = resources.create_initial_note_resource(
            s, space, title="BYO agent idea", first_note="Initial note"
        )

        assert first.external_path.startswith("resources/")
        assert "Initial note" in first.text_content
        assert (workspace / first.external_path).exists()

        summary_path = workspace / "resources" / "draft_summary.md"
        summary_path.write_text(
            "# Draft goal\n\nGoal content\n\n## Task one\n\nTask content\n",
            encoding="utf-8",
        )
        summary = resources.sync_draft_summary_file(s, space)

        assert summary is not None
        assert summary.type == "summary"
        assert summary.name == "Summary"
        assert summary.source == "terminal_agent"
        assert summary.external_path == "resources/draft_summary.md"

    with session_scope() as s:
        rows = s.query(InspirationResource).order_by(InspirationResource.id.asc()).all()
        assert [r.name for r in rows] == ["First Note", "Summary"]


def test_inspiration_resources_store_uploaded_bytes_rejects_empty():
    from openfocus.domains.inspirations import resources

    try:
        resources.store_uploaded_resource_bytes(
            space_id=1, seq_id=1, original_name="empty.png", content=b""
        )
    except ValueError as exc:
        assert "uploaded file is empty" in str(exc)
    else:
        raise AssertionError("empty uploaded resource should be rejected")


def test_inspiration_resources_draft_summary_tombstone_blocks_direct_resync():
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources, workspace
    from openfocus.models import InspirationResource, InspirationSpace

    with session_scope() as s:
        space = InspirationSpace(
            title="Deleted summary", status="open", mode="terminal"
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)

    summary_path = root / "resources" / "draft_summary.md"
    summary_path.write_text("# Goal\n\nBody\n\n## Task\n\nDo it\n", encoding="utf-8")
    synced = workspace.sync_resources(space_id)
    assert synced.item is not None
    summary_id = int(synced.item["id"])

    deleted = workspace.delete_resource(space_id, summary_id)
    assert deleted.resource_id == summary_id
    assert not summary_path.exists()

    summary_path.write_text("# Goal revived\n\nBody\n", encoding="utf-8")
    sync_again = workspace.sync_resources(space_id)
    assert sync_again.item is None
    assert "resources/draft_summary.md" not in {
        item.get("external_path") for item in sync_again.items
    }

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        direct = resources.sync_draft_summary_file(s, space)
        assert direct is None
        active_summaries = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == space_id)
            .filter(InspirationResource.type == "summary")
            .filter(InspirationResource.deleted_at.is_(None))
            .all()
        )
        assert active_summaries == []


def test_inspiration_resources_store_upload_rejects_symlinked_resources_root(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.inspirations import resources
    from openfocus.models import InspirationSpace

    external_dir = tmp_path / "outside-resources"
    external_dir.mkdir()

    with session_scope() as s:
        space = InspirationSpace(title="Symlink upload", status="open")
        s.add(space)
        s.flush()
        space_id = int(space.id)
        root = resources.workspace_path(space, space_id)
        space.workspace_path = str(root)
        (root / "resources").rmdir()
        (root / "resources").symlink_to(external_dir, target_is_directory=True)

        try:
            resources.store_uploaded_resource_bytes(
                space_id=space_id,
                seq_id=1,
                original_name="marker.png",
                content=b"marker",
                space=space,
            )
        except ValueError as exc:
            assert "unsafe resources directory" in str(exc)
        else:
            raise AssertionError("symlinked resources root should reject upload")

    assert not (external_dir / "resource_1.png").exists()
