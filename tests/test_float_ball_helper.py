# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt

from openfocus.companion import float_ball_helper as helper


def test_helper_sections_match_web_attention_buckets() -> None:
    summary = {
        "items": [
            {"id": 1, "bucket": "running", "title": "run"},
            {"id": 2, "bucket": "waiting", "title": "wait"},
            {"id": 3, "bucket": "next_move", "title": "next"},
        ],
        "buckets": {},
    }

    sections = helper._section_items(summary)

    assert [x["title"] for x in sections["running"]] == ["run"]
    assert [x["title"] for x in sections["waiting"]] == ["wait"]
    assert [x["title"] for x in sections["next_move"]] == ["next"]
    assert helper._counts(summary) == (1, 1)


def test_helper_waiting_uses_completed_bucket_for_web_compatibility() -> None:
    summary = {
        "buckets": {
            "running": [],
            "completed": [{"id": 10, "bucket": "waiting", "title": "review"}],
        }
    }

    sections = helper._section_items(summary)

    assert [x["title"] for x in sections["waiting"]] == ["review"]
    assert helper._counts(summary) == (0, 1)


def test_helper_primary_and_dismiss_urls_are_openfocus_absolute() -> None:
    item = {
        "action": {
            "primary_target": "agent_space",
            "primary_label": "Go to AgentSpace",
            "primary_url": "/tasks/task-public-id/agent_space",
        },
        "dismiss_url": "/api/agent_activity/items/7/dismiss",
    }

    label, path = helper._primary_action(item)

    assert label == "Go to AgentSpace"
    assert path == "/tasks/task-public-id/agent_space"
    assert (
        helper._absolute_url("http://127.0.0.1:8001/", path)
        == "http://127.0.0.1:8001/tasks/task-public-id/agent_space"
    )
    assert (
        helper._absolute_url("http://127.0.0.1:8001", helper._dismiss_path(item))
        == "http://127.0.0.1:8001/api/agent_activity/items/7/dismiss"
    )


def test_helper_labels_and_durations_follow_web_copy() -> None:
    now = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.timezone.utc)

    assert helper._state_label({"bucket": "running", "type": "running"}) == "Running"
    assert helper._state_label({"bucket": "next_move"}) == "Recommended"
    assert (
        helper._state_label(
            {"bucket": "waiting", "type": "waiting", "waiting_kind": "approval"}
        )
        == "Waiting approval"
    )
    assert (
        helper._duration_text(
            {"state_since": "2026-05-18T10:00:00+00:00"},
            now=now,
        )
        == "for 2h"
    )


def test_helper_visual_contract_is_dark_green_popover() -> None:
    assert helper.FLOAT_BALL_BG == "#064e3b"
    assert helper.READY_FILE_ENV == "OPENFOCUS_FLOAT_BALL_READY_FILE"
    assert helper.SUMMARY_PATH in helper.SWIFT_HELPER
    assert "signalReady()" in helper.SWIFT_HELPER
    assert "final class ClickSurface" in helper.SWIFT_HELPER
    assert "togglePopover" in helper.SWIFT_HELPER
    assert "openDashboard" in helper.SWIFT_HELPER
    assert 'normalizedURL("/goals")' in helper.SWIFT_HELPER
    assert "Dashboard" in helper.SWIFT_HELPER
    assert "NSClickGestureRecognizer" not in helper.SWIFT_HELPER
    assert "NextMove recommendations" in helper.SWIFT_HELPER
    assert "dismiss_url" in helper.SWIFT_HELPER
