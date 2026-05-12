# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import os


def test_memory_service_rotates_audit_and_creates_new_current(monkeypatch, tmp_path):
    from openfocus.domains.memory import service

    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
    cfg = service.MemoryConfig(
        audit_window_seconds=60, audit_max_entries=1, audit_ttl_days=7
    )
    now = dt.datetime(2026, 5, 11, 10, 0, 0, tzinfo=dt.timezone.utc)

    service.append_audit_entry(
        kind="goal.created",
        source="web",
        summary="Created goal from service test",
        occurred_at=now,
        cfg=cfg,
    )
    service.maintenance(now + dt.timedelta(minutes=2), cfg=cfg)

    audit_files = sorted((tmp_path / "memory" / "audit").glob("**/*.md"))
    daily_files = sorted((tmp_path / "memory" / "daily").glob("*.md"))

    assert len(audit_files) == 2
    assert daily_files
    daily_text = daily_files[0].read_text(encoding="utf-8")
    assert "Audit Window" in daily_text
    assert "Created goal from service test" in daily_text

    state = service.load_state_unlocked()
    current = state.get("current_audit") or {}
    assert current.get("entries") == 0
    assert current.get("rel_path")


def test_memory_service_ttl_only_cleans_expired_audit_files(monkeypatch, tmp_path):
    from openfocus.domains.memory import service

    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
    cfg = service.MemoryConfig(
        audit_window_seconds=3600, audit_max_entries=2000, audit_ttl_days=1
    )
    mem_dir = service.memory_dir()
    old_audit_dir = mem_dir / "audit" / "2026-05-09"
    old_audit_dir.mkdir(parents=True, exist_ok=True)
    old_audit = old_audit_dir / "2026-05-09_10-00-00.md"
    old_audit.write_text("old audit", encoding="utf-8")

    daily = mem_dir / "daily" / "2026-05-09.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text("daily must stay", encoding="utf-8")
    long_term = mem_dir / "MEMORY.md"
    long_term.write_text("long-term must stay", encoding="utf-8")

    old_ts = dt.datetime(2026, 5, 9, 10, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    os.utime(old_audit, (old_ts, old_ts))

    service.maintenance(
        dt.datetime(2026, 5, 11, 10, 0, 0, tzinfo=dt.timezone.utc), cfg=cfg
    )

    assert not old_audit.exists()
    assert daily.exists()
    assert "daily must stay" in daily.read_text(encoding="utf-8")
    assert long_term.exists()
    assert "long-term must stay" in long_term.read_text(encoding="utf-8")


def test_memory_service_rejects_path_traversal(monkeypatch, tmp_path):
    from openfocus.domains.memory import service

    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    try:
        service.path_from_rel("../outside.md")
    except ValueError as exc:
        assert "invalid memory path" in str(exc)
    else:
        raise AssertionError("path traversal should be rejected")
