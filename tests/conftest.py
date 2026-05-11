# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 允许直接 import 本仓库的 `openfocus/` 包（无需安装）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path):
    # 每个测试使用独立 SQLite 文件，避免互相污染
    os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "test.db")
    os.environ.pop("OPENFOCUS_DB_URL", None)

    # 清理 engine 缓存（如果之前有 import 过）
    try:
        from openfocus import db

        db.reset_engine()
    except Exception:
        pass

    # 初始化 schema（create_all + goals 轻量迁移）
    try:
        from openfocus.main import _startup

        _startup()
    except Exception:
        # 测试中如初始化失败，让后续用例暴露问题
        pass

    yield

    try:
        from openfocus import db

        db.reset_engine()
    except Exception:
        pass
