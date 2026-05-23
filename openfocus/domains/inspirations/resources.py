# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import mimetypes
import os
import re
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from ...models import InspirationResource, InspirationSpace
from .repository import InspirationResourceRepository


class InspirationResourceError(Exception):
    """Base class for inspiration resource domain errors."""


class DraftSummaryReadError(InspirationResourceError):
    """Raised when resources/draft_summary.md cannot be read."""


class EmptyDraftSummary(InspirationResourceError):
    """Raised when resources/draft_summary.md exists but has no content."""


class ResourceStorageError(InspirationResourceError, ValueError):
    """Raised when a resource file cannot be safely read or written."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def openfocus_data_root() -> Path:
    env_path = str(os.environ.get("OPENFOCUS_DB_PATH") or "").strip()
    if env_path:
        try:
            return Path(env_path).expanduser().resolve().parent
        except Exception:
            pass
    return Path(__file__).resolve().parents[3] / ".data"


def files_root() -> Path:
    root = openfocus_data_root() / "inspirations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def space_files_dir(space_id: int) -> Path:
    path = files_root() / f"space_{int(space_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_path(space: InspirationSpace | None, space_id: int) -> Path:
    raw = str(getattr(space, "workspace_path", "") or "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        path = space_files_dir(int(space_id))
    path.mkdir(parents=True, exist_ok=True)
    (path / "resources").mkdir(parents=True, exist_ok=True)
    return path


def resources_dir(space: InspirationSpace | None, space_id: int) -> Path:
    path = workspace_path(space, int(space_id)) / "resources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolved_resources_dir(
    space: InspirationSpace | None, space_id: int
) -> Path | None:
    root = resources_dir(space, int(space_id))
    if root.is_symlink():
        return None
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not root.is_dir():
        return None
    return resolved


def writable_resources_dir(space: InspirationSpace | None, space_id: int) -> Path:
    root = resources_dir(space, int(space_id))
    if resolved_resources_dir(space, int(space_id)) is None:
        raise ResourceStorageError("unsafe resources directory")
    return root


def safe_resource_file_path(
    space: InspirationSpace | None, space_id: int, path: Path | str
) -> Path | None:
    root_resolved = resolved_resources_dir(space, int(space_id))
    if root_resolved is None:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def safe_resource_filename(name: str, fallback: str) -> str:
    raw = str(name or "").strip() or str(fallback or "resource")
    raw = re.sub(r"[\\/\x00-\x1f]+", "-", raw)
    raw = re.sub(r"\s+", " ", raw).strip().strip(".")
    if not raw:
        raw = str(fallback or "resource")
    return raw[:160]


def resource_file_path(
    *, space: InspirationSpace | None, space_id: int, seq_id: int, name: str, ext: str
) -> Path:
    suffix = ext if str(ext or "").startswith(".") else f".{str(ext or 'txt')}"
    stem = safe_resource_filename(name, f"resource_{int(seq_id)}")
    return writable_resources_dir(space, int(space_id)) / (
        f"resource_{int(seq_id)}_{stem}{suffix}"
    )


def ensure_writable_resource_file_path(
    space: InspirationSpace | None, space_id: int, path: Path | str
) -> Path:
    root_resolved = resolved_resources_dir(space, int(space_id))
    if root_resolved is None:
        raise ResourceStorageError("unsafe resources directory")
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ResourceStorageError("unsafe resource file path")
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
        parent_resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as e:
        raise ResourceStorageError("unsafe resource file path") from e
    if candidate.exists() and not candidate.is_file():
        raise ResourceStorageError("unsafe resource file path")
    return candidate


def next_resource_seq(s: Session, space_id: int) -> int:
    return InspirationResourceRepository(s).next_seq(int(space_id))


def write_resource_file(
    resource: InspirationResource, space: InspirationSpace | None = None
) -> None:
    kind = str(getattr(resource, "type", "") or "text").strip().lower()
    sid = int(getattr(resource, "space_id", 0) or 0)
    seq = int(getattr(resource, "resource_seq_id", 0) or 0)
    if sid <= 0 or seq <= 0 or kind == "image":
        return
    name = str(getattr(resource, "name", "") or f"resource-{seq}")
    old_file_path = str(getattr(resource, "file_path", "") or "").strip()
    if kind == "url":
        path = resource_file_path(
            space=space, space_id=sid, seq_id=seq, name=name, ext=".url.md"
        )
        url = str(getattr(resource, "url_content", "") or "").strip()
        body = f"# {name}\n\nURL: {url}\n"
    else:
        path = resource_file_path(
            space=space, space_id=sid, seq_id=seq, name=name, ext=".md"
        )
        body = str(getattr(resource, "text_content", "") or "")
    path = ensure_writable_resource_file_path(space, sid, path)
    path.write_text(body, encoding="utf-8")
    resource.file_path = str(path)
    resource.external_path = str(path.relative_to(workspace_path(space, sid)))
    if old_file_path:
        old_path = safe_resource_file_path(space, sid, old_file_path)
        if old_path is not None and old_path != path:
            try:
                old_path.unlink()
            except Exception:
                pass


def create_initial_note_resource(
    s: Session, space: InspirationSpace, *, title: str, first_note: str
) -> InspirationResource:
    sid = int(space.id)
    clean_title = str(title or "Inspiration").strip() or "Inspiration"
    clean_note = str(first_note or "").strip()
    body = f"# {clean_title}\n"
    if clean_note:
        body += f"\n{clean_note}\n"
    resource = InspirationResource(
        space_id=sid,
        resource_seq_id=next_resource_seq(s, sid),
        type="text",
        name="First Note",
        text_content=body[:20000],
        source="create_space",
        is_system_generated=True,
    )
    s.add(resource)
    s.flush()
    write_resource_file(resource, space)
    s.add(resource)
    return resource


def store_uploaded_resource_bytes(
    *,
    space_id: int,
    seq_id: int,
    original_name: str,
    content: bytes,
    space: InspirationSpace | None = None,
) -> tuple[Path, str]:
    if not content:
        raise ValueError("uploaded file is empty")
    clean_name = str(original_name or "image")
    ext = Path(clean_name).suffix or ".bin"
    target_dir = writable_resources_dir(space, int(space_id))
    target_path = target_dir / f"resource_{int(seq_id)}{ext}"
    target_path = ensure_writable_resource_file_path(space, int(space_id), target_path)
    target_path.write_bytes(content)
    return target_path, clean_name[:512]


def guess_media_type(path: Path) -> str:
    guessed, _enc = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def resource_reference(res: InspirationResource) -> str:
    rid = int(getattr(res, "resource_seq_id", 0) or 0)
    name = str(getattr(res, "name", "") or f"resource-{rid}")
    kind = str(getattr(res, "type", "") or "resource")
    hint = ""
    if kind == "url":
        hint = str(getattr(res, "url_content", "") or "").strip()
    elif kind in {"text", "summary"}:
        body = str(getattr(res, "text_content", "") or "").strip()
        hint = body[:160] + ("…" if len(body) > 160 else "")
    else:
        hint = "Use this image as supporting context."
    return (f"[Resource #{rid}]\nName: {name}\nType: {kind}\nHint: {hint}").strip()


def resource_preview(res: InspirationResource) -> str:
    kind = str(getattr(res, "type", "") or "")
    if kind == "url":
        return str(getattr(res, "url_content", "") or "").strip()
    return str(getattr(res, "text_content", "") or "").strip()


def sync_draft_summary_file(
    s: Session, space: InspirationSpace
) -> InspirationResource | None:
    """Sync resources/draft_summary.md into a Summary resource.

    The terminal agent is an untrusted collaborator: this imports the file as a
    resource only and never creates Goal/Task rows.
    """

    sid = int(space.id)
    path = writable_resources_dir(space, sid) / "draft_summary.md"
    safe_path = safe_resource_file_path(space, sid, path)
    if safe_path is None:
        return None
    if is_resource_tombstoned(
        s, sid, external_path="resources/draft_summary.md", seq_id=None
    ):
        return None
    try:
        text_body = safe_path.read_text(encoding="utf-8")
    except Exception as e:
        raise DraftSummaryReadError(f"failed to read draft_summary.md: {e}") from e
    text_body = str(text_body or "").strip()
    if not text_body:
        raise EmptyDraftSummary("draft_summary.md is empty")

    existing = (
        s.query(InspirationResource)
        .filter(InspirationResource.space_id == sid)
        .filter(InspirationResource.deleted_at.is_(None))
        .filter(InspirationResource.type == "summary")
        .filter(InspirationResource.name.in_(["Summary", "Draft Summary"]))
        .order_by(InspirationResource.id.desc())
        .first()
    )
    if existing is not None and is_resource_tombstoned(
        s,
        sid,
        external_path="",
        seq_id=int(existing.resource_seq_id or 0),
    ):
        return None
    if existing is None:
        existing = InspirationResource(
            space_id=sid,
            resource_seq_id=next_resource_seq(s, sid),
            type="summary",
            name="Summary",
            source="terminal_agent",
            is_system_generated=True,
        )
        s.add(existing)
    existing.name = "Summary"
    existing.text_content = text_body[:20000]
    existing.file_path = str(safe_path)
    existing.external_path = "resources/draft_summary.md"
    existing.source = "terminal_agent"
    existing.is_system_generated = True
    existing.updated_at = utcnow()
    space.last_activity_at = utcnow()
    s.flush()
    return existing


def resource_tombstones(s: Session, space_id: int) -> tuple[set[str], set[int]]:
    rows = (
        s.query(
            InspirationResource.external_path,
            InspirationResource.resource_seq_id,
        )
        .filter(InspirationResource.space_id == int(space_id))
        .filter(InspirationResource.deleted_at.is_not(None))
        .all()
    )
    external_paths = {str(row[0] or "") for row in rows if str(row[0] or "").strip()}
    seq_ids = {int(row[1] or 0) for row in rows if int(row[1] or 0) > 0}
    return external_paths, seq_ids


def is_resource_tombstoned(
    s: Session, space_id: int, *, external_path: str, seq_id: int | None
) -> bool:
    external_paths, seq_ids = resource_tombstones(s, int(space_id))
    if str(external_path or "") in external_paths:
        return True
    return seq_id is not None and int(seq_id or 0) in seq_ids


def resource_name_from_path(path: Path) -> str:
    name = path.name
    if name == "draft_summary.md":
        return "Summary"
    if name.endswith(".url.md"):
        name = name[: -len(".url.md")]
    else:
        name = path.stem
    name = re.sub(r"^resource_\d+_?", "", name).strip() or path.stem
    return name[:512]


def parse_url_resource_file(path: Path) -> str:
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    for line in body.splitlines():
        cleaned = str(line or "").strip()
        if cleaned.lower().startswith("url:"):
            return cleaned.split(":", 1)[1].strip()[:4000]
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned[:4000]
    return ""


def sync_resources_dir(
    s: Session, space: InspirationSpace
) -> list[InspirationResource]:
    """Refresh InspirationResource rows from files under workspace/resources/."""

    sid = int(space.id)
    workspace = workspace_path(space, sid)
    root = resources_dir(space, sid)
    if resolved_resources_dir(space, sid) is None:
        return []
    synced: list[InspirationResource] = []
    tombstoned_external_paths, tombstoned_resource_seq_ids = resource_tombstones(s, sid)
    paths = sorted(
        safe_path
        for path in root.rglob("*")
        if (safe_path := safe_resource_file_path(space, sid, path)) is not None
    )
    for path in paths:
        try:
            external_path = str(path.relative_to(workspace))
        except Exception:
            continue
        if not external_path.startswith("resources/"):
            continue
        if external_path in tombstoned_external_paths:
            continue
        seq_match = re.match(r"^resource_(\d+)(?:_|\.)", path.name)
        if (
            seq_match is not None
            and int(seq_match.group(1)) in tombstoned_resource_seq_ids
        ):
            continue
        is_draft_summary = external_path == "resources/draft_summary.md"
        media_type = guess_media_type(path)
        if is_draft_summary:
            kind = "summary"
        elif str(media_type or "").startswith("image/"):
            kind = "image"
        elif path.name.endswith(".url.md"):
            kind = "url"
        else:
            kind = "text"

        body = ""
        url = ""
        if kind in {"text", "summary"}:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except Exception:
                continue
            if is_draft_summary and not str(body or "").strip():
                continue
        elif kind == "url":
            url = parse_url_resource_file(path)

        existing = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == sid)
            .filter(InspirationResource.external_path == external_path)
            .filter(InspirationResource.deleted_at.is_(None))
            .order_by(InspirationResource.id.desc())
            .first()
        )
        if existing is None and is_draft_summary:
            existing = (
                s.query(InspirationResource)
                .filter(InspirationResource.space_id == sid)
                .filter(InspirationResource.deleted_at.is_(None))
                .filter(InspirationResource.type == "summary")
                .filter(InspirationResource.name.in_(["Summary", "Draft Summary"]))
                .order_by(InspirationResource.id.desc())
                .first()
            )
        if existing is None:
            existing = InspirationResource(
                space_id=sid,
                resource_seq_id=next_resource_seq(s, sid),
                type=kind,
                name=resource_name_from_path(path),
                source="terminal_agent",
                is_system_generated=is_draft_summary,
            )
            s.add(existing)
        existing.type = kind
        existing.file_path = str(path)
        existing.external_path = external_path
        existing.deleted_at = None
        existing.updated_at = utcnow()
        if is_draft_summary:
            existing.name = "Summary"
            existing.source = "terminal_agent"
            existing.is_system_generated = True
        elif not str(existing.source or "").strip():
            existing.source = "terminal_agent"
        if kind in {"text", "summary"}:
            existing.text_content = body
            existing.url_content = ""
        elif kind == "url":
            existing.url_content = url
            existing.text_content = ""
        else:
            existing.text_content = ""
            existing.url_content = ""
        synced.append(existing)
    if synced:
        space.last_activity_at = utcnow()
    s.flush()
    return synced


def non_deleted_resources(
    s: Session, space_id: int, *, include_summary: bool = True
) -> list[InspirationResource]:
    return InspirationResourceRepository(s).list_active(
        int(space_id), include_summary=include_summary
    )


def clone_resource(
    *, s: Session, source: InspirationResource, target_space_id: int, seq_id: int
) -> InspirationResource:
    cloned = InspirationResource(
        space_id=int(target_space_id),
        resource_seq_id=int(seq_id),
        type=str(source.type or "text"),
        name=str(source.name or f"resource-{seq_id}"),
        text_content=str(source.text_content or ""),
        url_content=str(source.url_content or ""),
        file_path="",
        external_path="",
        source=str(getattr(source, "source", "") or "user"),
        is_system_generated=bool(source.is_system_generated),
    )
    if str(source.file_path or "").strip():
        src_path = Path(str(source.file_path or "")).expanduser()
        if src_path.exists() and src_path.is_file():
            target_dir = writable_resources_dir(None, int(target_space_id))
            ext = src_path.suffix or ""
            dst = target_dir / f"resource_{int(seq_id)}{ext}"
            dst = ensure_writable_resource_file_path(None, int(target_space_id), dst)
            shutil.copyfile(src_path, dst)
            cloned.file_path = str(dst)
            try:
                cloned.external_path = str(
                    dst.relative_to(workspace_path(None, int(target_space_id)))
                )
            except Exception:
                cloned.external_path = str(dst)
    elif str(cloned.type or "") in {"url", "text", "summary"}:
        write_resource_file(cloned, None)
    s.add(cloned)
    s.flush()
    return cloned
