"""兼容入口：转发到 skills/focus_report/scripts/focus_report.py。

建议直接使用 skill 目录下的脚本：
`python3 skills/focus_report/scripts/focus_report.py ...`
"""

from __future__ import annotations

from .focus_report.scripts.focus_report import main

if __name__ == "__main__":
    raise SystemExit(main())
