# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path


def _base_template() -> str:
    return Path("openfocus/templates/base.html").read_text(encoding="utf-8")


def test_attention_inbox_lives_in_nav_not_fixed_web_bubble() -> None:
    source = _base_template()

    assert 'id="attention-fab-wrap"' in source
    assert 'id="nav-attention"' not in source
    wrap_css = source[source.index(".attention-fab-wrap") : source.index(".attention-fab{")]
    assert "position: fixed" not in wrap_css
    assert "attention_bubble" not in source
    assert "attention-hide" not in source


def test_attention_inbox_has_system_float_ball_toggle_states() -> None:
    source = _base_template()

    assert 'id="attention-system-toggle"' in source
    assert "未启动" in source
    assert "启动中" in source
    assert "已启动" in source
    assert "/api/float_ball/start" in source
    assert "/api/float_ball/stop" in source
    assert "Starting System Inbox..." in source


def test_attention_inbox_uses_standard_nav_button_style() -> None:
    source = _base_template()
    style = source[source.index(".attention-fab{") : source.index(".attention-fab:hover")]

    assert "background: transparent" in style
    assert "color: var(--muted-foreground)" in style
    assert "border-radius: calc(var(--radius) - 2px)" in style
    assert "box-shadow: none" in style
    assert "linear-gradient" not in style
