# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest


def test_create_prompt_trims_and_returns_payload():
    from openfocus.domains.agent_spaces import prompts

    result = prompts.create_prompt(
        title="  Review changes  ",
        content="  Review the current diff.  ",
        enabled=True,
        auto_enabled=True,
    )

    assert result.item.title == "Review changes"
    assert result.item.content == "Review the current diff."
    assert result.item.enabled is True
    assert result.item.auto_enabled is True
    assert result.to_dict() == {
        "ok": True,
        "item": {
            "id": result.item.id,
            "title": "Review changes",
            "content": "Review the current diff.",
            "enabled": True,
            "auto_enabled": True,
        },
    }


def test_default_list_hides_disabled_prompts():
    from openfocus.domains.agent_spaces import prompts

    prompts.create_prompt(
        title="Visible",
        content="Show this prompt.",
        enabled=True,
        auto_enabled=False,
    )
    prompts.create_prompt(
        title="Hidden",
        content="Hide this prompt.",
        enabled=False,
        auto_enabled=False,
    )

    result = prompts.list_prompts()

    assert [item.title for item in result.items] == ["Visible"]
    assert result.to_dict()["items"][0]["title"] == "Visible"


def test_list_prompts_with_enabled_only_false_shows_disabled_prompts():
    from openfocus.domains.agent_spaces import prompts

    first = prompts.create_prompt(
        title="First",
        content="First prompt.",
        enabled=True,
        auto_enabled=False,
    )
    second = prompts.create_prompt(
        title="Second",
        content="Second prompt.",
        enabled=False,
        auto_enabled=True,
    )

    result = prompts.list_prompts(enabled_only=False)

    assert [item.id for item in result.items] == [second.item.id, first.item.id]
    assert [item.enabled for item in result.items] == [False, True]


def test_update_and_toggle_missing_prompt_return_not_found_domain_error():
    from openfocus.domains.agent_spaces import prompts

    with pytest.raises(prompts.AgentSpacePromptNotFound):
        prompts.update_prompt(
            404,
            title="Missing",
            content="Missing prompt.",
            enabled=True,
            auto_enabled=False,
        )

    with pytest.raises(prompts.AgentSpacePromptNotFound):
        prompts.set_prompt_enabled(404, enabled=True)

    with pytest.raises(prompts.AgentSpacePromptNotFound):
        prompts.set_prompt_auto_enabled(404, auto_enabled=True)


@pytest.mark.parametrize(
    ("title", "content", "message"),
    [
        ("  ", "Valid content.", "title and content are required"),
        ("Valid title", "\n\t", "title and content are required"),
        ("x" * 161, "Valid content.", "title is too long"),
        ("Valid title", "x" * 20001, "content is too long"),
    ],
)
def test_invalid_prompt_values_return_validation_error(title, content, message):
    from openfocus.domains.agent_spaces import prompts

    with pytest.raises(prompts.AgentSpacePromptValidationError, match=message):
        prompts.create_prompt(
            title=title,
            content=content,
            enabled=True,
            auto_enabled=False,
        )


def test_delete_missing_prompt_is_idempotent_success():
    from openfocus.domains.agent_spaces import prompts

    result = prompts.delete_prompt(404)

    assert result.to_dict() == {"ok": True}
