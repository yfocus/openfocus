from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def _default_sqlite_url() -> str:
    # 优先使用显式 DB URL（例如测试时用 sqlite:///:memory:）
    env_url = os.environ.get("OPENFOCUS_DB_URL")
    if env_url:
        return env_url

    # 其次允许指定 DB 文件路径（便于测试隔离）
    env_path = os.environ.get("OPENFOCUS_DB_PATH")
    if env_path:
        db_path = Path(env_path).expanduser().resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"

    # 默认：项目根目录下 .data/openfocus.db
    data_dir = Path(__file__).resolve().parent.parent / ".data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "openfocus.db"
    return f"sqlite:///{db_path}"


_engine: Engine | None = None
_engine_url: str | None = None


def get_engine() -> Engine:
    """获取（并缓存）SQLAlchemy Engine。

    测试场景下可通过环境变量切换 DB（OPENFOCUS_DB_URL/OPENFOCUS_DB_PATH）。
    """
    global _engine, _engine_url
    url = _default_sqlite_url()
    if _engine is not None and _engine_url == url:
        return _engine

    if _engine is not None:
        _engine.dispose()

    connect_args = {}
    if url.startswith("sqlite:"):
        connect_args = {"check_same_thread": False}

    _engine = create_engine(url, connect_args=connect_args)
    _engine_url = url
    return _engine


def reset_engine() -> None:
    """测试用：清空 engine 缓存。"""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


@contextmanager
def session_scope() -> Session:
    # 供模板渲染使用：避免 commit 后对象过期导致 DetachedInstanceError
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
