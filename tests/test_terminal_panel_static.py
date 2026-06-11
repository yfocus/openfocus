# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


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


def test_agent_space_task_panel_and_layout_settings_are_exposed():
    source = open(
        "openfocus/static/terminal-panel/terminal.js", encoding="utf-8"
    ).read()
    css = open("openfocus/static/terminal-panel/terminal.css", encoding="utf-8").read()
    agent_space_template = open(
        "openfocus/templates/agent_space.html", encoding="utf-8"
    ).read()
    base_template = open("openfocus/templates/base.html", encoding="utf-8").read()
    agent_space_react = Path("frontend/src/entries/agent-space.tsx").read_text(
        encoding="utf-8"
    )

    assert 'id="rt-task-show"' in source
    assert 'id="rt-task-details-modal"' in source
    assert 'id="rt-task-cleanup"' in source
    assert 'id="rt-start-settings-modal"' in source
    assert 'id="rt-start-command-input"' in source
    assert 'id="rt-show-files"' in source
    assert 'id="rt-show-preview"' in source
    assert 'id="rt-show-terminal"' in source
    assert 'data-rt-settings-tab="general"' in source
    assert 'data-rt-settings-tab="appearance"' in source
    assert 'data-rt-settings-tab="shortcuts"' in source
    assert 'id="rt-shortcut-list"' in source
    assert "openfocus.agent_space.shortcuts.v1" in source
    assert "search_everywhere" in source
    assert "Search Everywhere" in source
    assert "find_in_files" in source
    assert "Find in Files" in source
    assert "go_to_definition" in source
    assert "Go to Definition" in source
    assert "find_usages" in source
    assert "Find Usages" in source
    assert "navigation_back" in source
    assert "Navigate Back" in source
    assert "navigation_forward" in source
    assert "Navigate Forward" in source
    assert "AGENT_SPACE_UNAVAILABLE_SHORTCUT_COMMAND_IDS" in source
    assert "new Set([])" in source
    unavailable_line = next(
        line
        for line in source.splitlines()
        if "AGENT_SPACE_UNAVAILABLE_SHORTCUT_COMMAND_IDS" in line
    )
    assert "go_to_definition" not in unavailable_line
    assert "find_usages" not in unavailable_line
    assert "navigation_back" not in unavailable_line
    assert "navigation_forward" not in unavailable_line
    assert "Not available yet" in source
    assert 'disabled aria-disabled="true" title="Not available yet"' in source
    assert "focus_files" in source
    assert "Focus Files" in source
    assert "focus_preview" in source
    assert "Focus Preview" in source
    assert "focus_terminal" in source
    assert "Focus Terminal" in source
    assert "Double Shift" in source
    assert "if(key === 'Alt') return platform === 'mac' ? 'Option' : 'Alt';" in source
    assert "Press shortcut" in source
    assert "recordingCurrent" in source
    assert "formatShortcutBinding(recordingCapturedBinding, platform)" in source
    assert "if(!confirmShortcutRecording({ saving: true })) return ''" in source
    assert "Press shortcut or Escape to cancel recording before saving." in source
    assert "Browser-reserved shortcut" in source
    assert ".rt-settings-tabs" in css
    assert ".rt-shortcut-row" in css
    assert 'data-rt-pane="${esc(pane)}"' in source
    assert "toggleAgentSpacePane" in source
    assert (
        "showFiles" in source and "showPreview" in source and "showTerminal" in source
    )
    assert "sideRoot" in source
    assert 'id="rt-start-agent-edit"' in source
    assert "openfocus.agent_space.settings.v1" in source
    assert "prompt('Start Agent command'" not in source
    assert ".rt-task-panel" in css
    assert ".rt-settings-panel" in css
    assert ".rt-pane-icon.on" in css
    assert ".rt-pane-toggles" in css
    assert '.rt-side[data-terminal-open="0"] .rt-prompt-zone' in css
    assert "space-copy-task" not in agent_space_template
    assert "agent-space-settings-column" in agent_space_react
    assert "MarkdownPreview" in agent_space_react
    assert "imageSrcForPath" in agent_space_react
    assert "onOpenWorkspacePath" in agent_space_react
    assert "Show Markdown source" in agent_space_react
    assert "Render Markdown" in agent_space_react
    assert ".markdown-preview" in agent_space_template
    assert "/static/dist/assets/agent-space.js" in agent_space_template
    assert "asset=4" in agent_space_template
    assert ".markdown-preview a { color: #67e8f9;" in agent_space_template
    assert ".markdown-preview code" in agent_space_template
    assert (
        ".markdown-preview code { font-family: var(--mono); font-size: 0.92em; color: var(--accent);"
        not in agent_space_template
    )
    assert ".markdown-preview table" in agent_space_template
    assert ".markdown-preview img" in agent_space_template
    assert 'id="nav-system"' in base_template
    assert 'id="system-dialog"' in base_template
    assert "system-files-font-size" in base_template
    assert "openfocus.agent_space.settings.v1" in base_template


def test_agent_space_prompt_master_frontend_hooks_are_exposed():
    source = open(
        "openfocus/static/terminal-panel/terminal.js", encoding="utf-8"
    ).read()
    css = open("openfocus/static/terminal-panel/terminal.css", encoding="utf-8").read()

    assert "const promptMasterHtml = isInspiration ? ''" in source
    assert 'id="rt-prompt-master-open"' in source
    assert ">Prompt Master</button>" in source
    assert 'id="rt-prompt-master-panel"' in source
    assert 'id="rt-prompt-master-text"' in source
    assert 'id="rt-prompt-master-optimize">optimize</button>' in source
    assert 'id="rt-prompt-master-send">send</button>' in source
    assert 'id="rt-prompt-master-cancel">cancel</button>' in source
    assert source.index('id="rt-prompt-master-open"') < source.index(
        'id="rt-start-agent"'
    )

    assert (
        "`/api/agent_spaces/${spaceId}/prompt_master/optimize`"
        in source
    )
    assert "body: JSON.stringify({ prompt: text })" in source
    assert "if(promptMasterText && 'value' in promptMasterText) promptMasterText.value = next" in source
    assert "toast('Prompt optimized')" in source
    assert (
        "injectPromptToTerminal(activeTerminal(), text, { bracketedPaste: true, submit: false, focus: true })"
        in source
    )
    assert "closePromptMasterMode();" in source
    assert "btnPromptMasterCancel?.addEventListener('click', closePromptMasterMode)" in source
    assert "Enter a prompt first" in source

    assert ".rt-side.rt-prompt-master-mode .rt-task-panel" in css
    assert ".rt-side.rt-prompt-master-mode .rt-prompt-zone" in css
    assert ".rt-prompt-master-actions" in css
    assert "grid-template-columns:repeat(3, minmax(0, 1fr))" in css
    assert '.rt-side[data-terminal-open="0"] .rt-prompt-zone' in css
    assert '.rt-side[data-terminal-open="0"] .rt-prompt-master-panel' not in css
    assert "if(isInspiration || !promptMasterPanel) return;" in source
    assert "el.classList.add('rt-prompt-master-mode')" in source
    assert "promptMasterPanel.hidden = false;" in source
