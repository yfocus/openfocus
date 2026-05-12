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
