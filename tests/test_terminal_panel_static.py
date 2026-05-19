# SPDX-License-Identifier: Apache-2.0


def test_agent_space_prompt_zone_exposes_pua_button_and_prompt():
    source = open(
        "openfocus/static/terminal-panel/terminal.js", encoding="utf-8"
    ).read()

    assert 'id="rt-pua"' in source
    assert "PUA_PROACTIVITY_PROMPT" in source
    assert "https://mcpmarket.com/tools/skills/pua-proactivity-engine" in source
    assert "buildPasteText('pua')" in source
    assert 'id="rt-send-basic"' in source
    assert "send basic" in source
    assert "buildPasteText('task_basic')" in source
    assert "if(k === 'task_basic') return taskBasic" in source
    assert 'data-auto-builtin="task_basic"' in source
    assert "loadBuiltinAutoPrompt('task_basic')" in source
    assert "autoStartDefaultTerminal" in source
    assert "maybeAutoStartDefaultTerminal(it)" in source
    assert 'id="rt-report-progress"' in source
    assert "report progress" in source
    assert "buildPasteText('report_progress')" in source
    assert "draw lessons" not in source
    assert "OpenFocus Lessons" not in source
    assert 'data-auto-builtin="pua"' in source
    assert 'data-auto-builtin="report_progress"' in source
    assert "/auto_prompts" in source
    assert "You are a P8-level senior engineer" in source
    assert "if(k === 'pua') return PUA_PROACTIVITY_PROMPT" in source
    assert (
        "Auto-injection failed. Prompt copied to clipboard; paste it into the terminal manually."
        in source
    )
    assert "主动性升级模式" not in source
    assert "`${PUA_PROACTIVITY_PROMPT}\\n" not in source

    goals_source = open("openfocus/templates/goals.html", encoding="utf-8").read()
    assert "startAgentCommand ? '?autostart=1' : ''" in goals_source
