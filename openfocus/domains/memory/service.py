# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .filesystem import append_text, read_text, write_text_atomic

_LOCK = threading.RLock()


@dataclass(frozen=True)
class MemoryConfig:
    audit_window_seconds: int = 3600
    audit_max_entries: int = 2000
    audit_ttl_days: int = 7


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utcnow() -> dt.datetime:
    return _utcnow()


def config_from_env() -> MemoryConfig:
    def _read_int(name: str, default: int, min_value: int) -> int:
        raw = str(os.environ.get(name) or "").strip()
        try:
            return max(min_value, int(raw or default))
        except Exception:
            return default

    return MemoryConfig(
        audit_window_seconds=_read_int(
            "OPENFOCUS_MEMORY_AUDIT_WINDOW_SECONDS", 3600, 60
        ),
        audit_max_entries=_read_int("OPENFOCUS_MEMORY_AUDIT_MAX_ENTRIES", 2000, 1),
        audit_ttl_days=_read_int("OPENFOCUS_MEMORY_AUDIT_TTL_DAYS", 7, 1),
    )


def memory_dir() -> Path:
    env = os.environ.get("OPENFOCUS_MEMORY_DIR")
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = (
            Path(__file__).resolve().parent.parent.parent.parent / ".data" / "memory"
        ).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def audit_root() -> Path:
    p = memory_dir() / "audit"
    p.mkdir(parents=True, exist_ok=True)
    return p


def daily_root() -> Path:
    p = memory_dir() / "daily"
    p.mkdir(parents=True, exist_ok=True)
    return p


def long_term_path() -> Path:
    return memory_dir() / "MEMORY.md"


def state_path() -> Path:
    return memory_dir() / ".memory_state.json"


def path_from_rel(rel_path: str) -> Path:
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    p = (memory_dir() / rel).resolve()
    base = memory_dir().resolve()
    if p != base and base not in p.parents:
        raise ValueError("invalid memory path")
    return p


def rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(memory_dir().resolve()).as_posix()
    except Exception:
        return path.name


def iso(ts: dt.datetime | None) -> str:
    if ts is None:
        ts = _utcnow()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        raw = raw.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def decode_terminal_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def load_state_unlocked() -> dict:
    raw = read_text(state_path()).strip()
    if not raw:
        return {
            "current_audit": None,
            "summarized_audits": [],
            "finalized_days": [],
            "last_maintenance_at": None,
        }
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    summarized = data.get("summarized_audits")
    data["summarized_audits"] = [
        str(x)
        for x in (summarized if isinstance(summarized, list) else [])
        if str(x).strip()
    ]
    finalized = data.get("finalized_days")
    data["finalized_days"] = [
        str(x)
        for x in (finalized if isinstance(finalized, list) else [])
        if str(x).strip()
    ]
    if not isinstance(data.get("current_audit"), dict):
        data["current_audit"] = None
    return data


def save_state_unlocked(state: dict) -> None:
    payload = {
        "current_audit": state.get("current_audit"),
        "summarized_audits": list(
            dict.fromkeys(
                [str(x) for x in state.get("summarized_audits") or [] if str(x).strip()]
            )
        ),
        "finalized_days": list(
            dict.fromkeys(
                [str(x) for x in state.get("finalized_days") or [] if str(x).strip()]
            )
        ),
        "last_maintenance_at": state.get("last_maintenance_at"),
    }
    write_text_atomic(state_path(), json.dumps(payload, ensure_ascii=False, indent=2))


def extract_json_blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"```json\n(.*?)\n```", text or "", flags=re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def entry_markdown(entry: dict) -> str:
    ts = str(entry.get("timestamp") or iso(None))
    kind = str(entry.get("kind") or "memory.event")
    source = str(entry.get("source") or "system")
    summary = str(entry.get("summary") or kind).strip()
    detail = str(entry.get("detail") or "").strip()
    task_id = str(entry.get("task_public_id") or "").strip()
    goal_id = entry.get("goal_id")
    lines = [f"## {ts} · {kind}", f"- Source: {source}", f"- Summary: {summary}"]
    if task_id:
        lines.append(f"- Task: {task_id}")
    if goal_id not in (None, ""):
        lines.append(f"- Goal: {goal_id}")
    if detail:
        lines.append("")
        lines.append(detail)
    lines.extend(
        ["", "```json", json.dumps(entry, ensure_ascii=False, indent=2), "```", ""]
    )
    return "\n".join(lines)


def render_audit_header(*, started_at: dt.datetime, cfg: MemoryConfig) -> str:
    return (
        "# Audit Memory\n\n"
        f"- Started At: {iso(started_at)}\n"
        f"- Rotation: {int(cfg.audit_window_seconds / 60)} minutes or {cfg.audit_max_entries} entries\n"
        f"- TTL: {cfg.audit_ttl_days} days\n\n"
        "---\n\n"
    )


def render_daily_summary(
    *,
    day: str,
    file_label: str,
    started_at: str,
    ended_at: str,
    entries: list[dict],
) -> str:
    counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    highlights: list[str] = []
    for entry in entries:
        kind = str(entry.get("kind") or "memory.event")
        counts[kind] = counts.get(kind, 0) + 1
        source = str(entry.get("source") or "system")
        source_counts[source] = source_counts.get(source, 0) + 1
        summary = str(entry.get("summary") or "").strip()
        if summary and summary not in highlights:
            highlights.append(summary)

    top_kinds = sorted(counts.items(), key=lambda it: (-it[1], it[0]))[:5]
    top_sources = sorted(source_counts.items(), key=lambda it: (-it[1], it[0]))[:5]
    lines = [
        f"## Audit Window · {file_label}",
        f"- Start: {started_at}",
        f"- End: {ended_at}",
        f"- Entries: {len(entries)}",
    ]
    if top_sources:
        lines.append(
            "- Sources: "
            + ", ".join(f"{name} ({count})" for name, count in top_sources)
        )
    if top_kinds:
        lines.append(
            "- Top Kinds: "
            + ", ".join(f"{name} ({count})" for name, count in top_kinds)
        )
    if highlights:
        lines.append("")
        lines.append("### Highlights")
        for item in highlights[:8]:
            lines.append(f"- {item}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def render_daily_final(day: str, content: str) -> str:
    lines = [ln.rstrip() for ln in (content or "").splitlines()]
    cleaned = [ln for ln in lines if ln.strip()]
    highlights: list[str] = []
    for ln in cleaned:
        if ln.startswith("- "):
            bullet = ln[2:].strip()
            if bullet and bullet not in highlights:
                highlights.append(bullet)
        if len(highlights) >= 12:
            break
    out = [f"# Daily Memory · {day}", "", f"- Finalized At: {iso(None)}", ""]
    if highlights:
        out.append("## Final Highlights")
        for item in highlights[:12]:
            out.append(f"- {item}")
        out.append("")
    out.append("## Source Material")
    out.append("")
    out.append(content.strip() or "No daily material.")
    out.append("")
    return "\n".join(out)


def extract_long_term_items(day: str, daily_text: str) -> list[str]:
    items: list[str] = []
    text = daily_text or ""
    lower = text.lower()
    if "trae-cli" in lower:
        items.append(f"- {day}: Uses `trae-cli` in AgentSpace workflows.")
    if "terminal" in lower or "web shell" in lower:
        items.append(
            f"- {day}: Works through AgentSpace terminal / web shell interactions."
        )
    if not items:
        return [f"- {day}: No stable preference or fact extracted yet."]
    return items


def write_long_term_unlocked(*, day: str, items: list[str]) -> None:
    path = long_term_path()
    existing = read_text(path).strip()
    kept: list[str] = []
    if existing:
        for ln in existing.splitlines():
            stripped = ln.rstrip()
            if stripped.startswith(f"- {day}:"):
                continue
            kept.append(stripped)
    else:
        kept = ["# Long-term Memory", "", "## Stable Facts", ""]

    if not any((ln.strip() == "## Stable Facts") for ln in kept):
        if kept and kept[-1] != "":
            kept.append("")
        kept.extend(["## Stable Facts", ""])
    if kept and kept[-1] != "":
        kept.append("")
    kept.extend(items)
    kept.append("")
    write_text_atomic(path, "\n".join(kept).rstrip() + "\n")


def cleanup_audit_files_unlocked(now: dt.datetime, cfg: MemoryConfig) -> None:
    cutoff = now - dt.timedelta(days=cfg.audit_ttl_days)
    for path in sorted(audit_root().glob("**/*.md")):
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        except Exception:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for day_dir in sorted(audit_root().glob("*")):
        try:
            if day_dir.is_dir() and not any(day_dir.iterdir()):
                day_dir.rmdir()
        except Exception:
            pass


def ensure_daily_file(day: str) -> Path:
    path = daily_root() / f"{day}.md"
    if not path.exists():
        write_text_atomic(path, f"# Daily Memory · {day}\n\n")
    return path


def start_audit_file_unlocked(state: dict, now: dt.datetime, cfg: MemoryConfig) -> dict:
    day = now.date().isoformat()
    day_dir = audit_root() / day
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    path = day_dir / filename
    counter = 1
    while path.exists():
        filename = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{counter}.md"
        path = day_dir / filename
        counter += 1
    write_text_atomic(path, render_audit_header(started_at=now, cfg=cfg))
    current = {
        "rel_path": rel_path(path),
        "started_at": iso(now),
        "entries": 0,
        "day": day,
    }
    state["current_audit"] = current
    return current


def mark_summarized_audit_unlocked(state: dict, rel: str) -> None:
    rel = str(rel or "").strip()
    if not rel:
        return
    items = [
        str(x)
        for x in state.get("summarized_audits") or []
        if str(x).strip() and str(x).strip() != rel
    ]
    items.append(rel)
    state["summarized_audits"] = items[-2000:]


def finalize_day_unlocked(day: str, state: dict) -> None:
    path = daily_root() / f"{day}.md"
    if not path.exists():
        return
    current = read_text(path)
    finalized = render_daily_final(day, current)
    write_text_atomic(path, finalized)
    write_long_term_unlocked(day=day, items=extract_long_term_items(day, finalized))
    finalized_days = [
        str(x)
        for x in state.get("finalized_days") or []
        if str(x).strip() and str(x) != day
    ]
    finalized_days.append(day)
    state["finalized_days"] = finalized_days


def finalize_due_days_unlocked(state: dict, now: dt.datetime) -> None:
    today = now.date().isoformat()
    finalized = {str(x) for x in state.get("finalized_days") or [] if str(x).strip()}
    for path in sorted(daily_root().glob("*.md")):
        day = path.stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if day >= today or day in finalized:
            continue
        finalize_day_unlocked(day, state)


def rotate_current_audit_unlocked(
    state: dict,
    now: dt.datetime,
    *,
    cfg: MemoryConfig,
    force: bool = False,
    create_next: bool = True,
) -> tuple[str | None, str | None]:
    current = (
        state.get("current_audit")
        if isinstance(state.get("current_audit"), dict)
        else None
    )
    if not current:
        return None, None
    started_at = parse_ts(current.get("started_at")) or now
    entries = int(current.get("entries") or 0)
    age_seconds = max(0, int((now - started_at).total_seconds()))
    if (
        (not force)
        and entries < cfg.audit_max_entries
        and age_seconds < cfg.audit_window_seconds
    ):
        return None, None
    rel = str(current.get("rel_path") or "").strip()
    if not rel:
        state["current_audit"] = None
        return None, None
    if entries <= 0:
        return None, None

    path = path_from_rel(rel)
    if path.exists():
        entries_data = extract_json_blocks(read_text(path))
        day = str(current.get("day") or started_at.date().isoformat())
        daily_path = ensure_daily_file(day)
        started_iso = iso(started_at)
        ended_iso = iso(now)
        summary = render_daily_summary(
            day=day,
            file_label=path.name,
            started_at=started_iso,
            ended_at=ended_iso,
            entries=entries_data,
        )
        append_text(daily_path, summary)
        mark_summarized_audit_unlocked(state, rel)
        state["finalized_days"] = [
            str(x) for x in state.get("finalized_days") or [] if str(x) != day
        ]
    state["current_audit"] = None

    next_rel: str | None = None
    if create_next:
        next_rel = (
            str(
                start_audit_file_unlocked(state, now, cfg).get("rel_path") or ""
            ).strip()
            or None
        )
    return rel, next_rel


def maintenance(
    now: dt.datetime | None = None, *, cfg: MemoryConfig | None = None
) -> None:
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    cfg = cfg or config_from_env()
    with _LOCK:
        state = load_state_unlocked()
        rotate_current_audit_unlocked(
            state, now, cfg=cfg, force=False, create_next=True
        )
        finalize_due_days_unlocked(state, now)
        cleanup_audit_files_unlocked(now, cfg)
        state["last_maintenance_at"] = iso(now)
        save_state_unlocked(state)


def append_audit_entry(
    *,
    kind: str,
    source: str,
    summary: str,
    detail: str = "",
    task_public_id: str | None = None,
    goal_id: int | None = None,
    metadata: dict | None = None,
    occurred_at: dt.datetime | None = None,
    cfg: MemoryConfig | None = None,
) -> None:
    now = occurred_at or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    cfg = cfg or config_from_env()
    entry = {
        "timestamp": iso(now),
        "kind": str(kind or "memory.event"),
        "source": str(source or "system"),
        "summary": str(summary or kind or "memory event").strip(),
        "detail": str(detail or "").strip(),
        "task_public_id": str(task_public_id or "").strip() or None,
        "goal_id": goal_id,
        "metadata": metadata or {},
    }

    with _LOCK:
        state = load_state_unlocked()
        rotate_current_audit_unlocked(
            state, now, cfg=cfg, force=False, create_next=True
        )
        current = (
            state.get("current_audit")
            if isinstance(state.get("current_audit"), dict)
            else None
        )
        if current is None:
            current = start_audit_file_unlocked(state, now, cfg)
        path = path_from_rel(str(current.get("rel_path") or ""))
        append_text(path, entry_markdown(entry))
        current["entries"] = int(current.get("entries") or 0) + 1
        state["current_audit"] = current
        finalize_due_days_unlocked(state, now)
        cleanup_audit_files_unlocked(now, cfg)
        state["last_maintenance_at"] = iso(now)
        save_state_unlocked(state)


def try_audit_memory(**kwargs) -> None:
    try:
        append_audit_entry(**kwargs)
    except Exception:
        pass


def file_display_name(path: Path) -> str:
    if path.suffix.lower() == ".md":
        stem = path.stem
        if re.fullmatch(r"\d{2}-\d{2}-\d{2}", stem):
            day = path.parent.name
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return f"{day} {stem.replace('-', ':')}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", stem):
            day2, tm = stem.split("_", 1)
            return f"{day2} {tm.replace('-', ':')}"
    return path.name


def collect_file_items(root: Path, pattern: str) -> list[dict]:
    state = load_state_unlocked()
    summarized = {
        str(x) for x in state.get("summarized_audits") or [] if str(x).strip()
    }
    current_rel = ""
    if isinstance(state.get("current_audit"), dict):
        current_rel = str(
            (state.get("current_audit") or {}).get("rel_path") or ""
        ).strip()

    items: list[dict] = []
    for path in sorted(root.glob(pattern), reverse=True):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            updated_at = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
            size = int(stat.st_size)
        except Exception:
            updated_at = _utcnow()
            size = 0
        relp = rel_path(path)
        items.append(
            {
                "name": file_display_name(path),
                "rel_path": relp,
                "updated_at": iso(updated_at),
                "size": size,
                "summarized": relp in summarized,
                "current": relp == current_rel,
            }
        )
    return items


def read_selected_file(relp: str | None) -> str:
    raw = str(relp or "").strip()
    if not raw:
        return ""
    try:
        return read_text(path_from_rel(raw))
    except Exception:
        return ""


def persist_feedback_learning(
    *, note: str, memory_note: str | None = None, now: dt.datetime | None = None
) -> None:
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    with _LOCK:
        daily_path = daily_root() / f"{now.date().isoformat()}.md"
        existing_daily = read_text(daily_path)
        if note and note not in existing_daily:
            prefix = (
                "\n## Next Move Feedback\n\n"
                if "## Next Move Feedback" not in existing_daily
                else "\n"
            )
            append_text(daily_path, prefix + note + "\n")

        if memory_note:
            path = long_term_path()
            existing_long_term = read_text(path)
            if memory_note not in existing_long_term:
                prefix = (
                    "\n## Learned Preferences\n\n"
                    if "## Learned Preferences" not in existing_long_term
                    else "\n"
                )
                append_text(path, prefix + memory_note + "\n")
