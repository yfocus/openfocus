# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, AsyncIterator, Iterable

from ...companion.grpc import CompanionGrpcError
from ..companion import service as companion_service

SEARCH_BACKEND = "text_fallback"
SYMBOL_BACKEND = "symbol_fallback"
DEFINITION_BACKEND = "definition_fallback"
REFERENCE_BACKEND = "reference_fallback"

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_QUERY_LENGTH = 500
MAX_SYMBOL_LENGTH = 200
MAX_TEXT_FILE_BYTES = 1024 * 1024
MAX_WORKSPACE_VISITED_PATHS = 5000
MAX_WORKSPACE_READ_FILES = 1000

DEFAULT_EXCLUDES = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
    "__pycache__",
)

SEARCH_KINDS = frozenset({"all", "file", "text", "symbol"})
TEXT_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".json",
        ".md",
        ".php",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class CodeNavigationUseCaseError(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class CodeNavigationValidationError(CodeNavigationUseCaseError):
    pass


@dataclass(frozen=True)
class _FileItem:
    path: str
    content: str
    truncated: bool


@dataclass
class _ResultCollector:
    limit: int
    results: list[dict]
    truncated: bool = False

    def add(self, item: dict) -> bool:
        if len(self.results) >= self.limit:
            self.truncated = True
            return False
        self.results.append(item)
        if len(self.results) >= self.limit:
            self.truncated = True
            return False
        return True


@dataclass
class _TraversalBudget:
    max_visited_paths: int
    max_read_files: int
    visited_paths: int = 0
    read_files: int = 0
    truncated: bool = False

    def consume_path(self) -> bool:
        if self.visited_paths >= self.max_visited_paths:
            self.truncated = True
            return False
        self.visited_paths += 1
        return True

    def consume_read(self) -> bool:
        if self.read_files >= self.max_read_files:
            self.truncated = True
            return False
        self.read_files += 1
        return True


class _SearchMatcher:
    def __init__(self, query: str, *, case_sensitive: bool, regex: bool) -> None:
        self.query = query
        self.case_sensitive = case_sensitive
        self.regex = regex
        self._needle = query if case_sensitive else query.lower()
        self._compiled: re.Pattern[str] | None = None
        if regex and query:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                self._compiled = re.compile(query, flags)
            except re.error as exc:
                raise CodeNavigationValidationError(f"invalid regex: {exc}") from exc

    def search(self, value: str) -> re.Match[str] | None:
        text = str(value or "")
        if not self.query:
            return None
        if self._compiled is not None:
            return self._compiled.search(text)
        haystack = text if self.case_sensitive else text.lower()
        index = haystack.find(self._needle)
        if index < 0:
            return None
        return _PlainMatch(index, index + len(self.query))


class _PlainMatch:
    def __init__(self, start: int, end: int) -> None:
        self._start = int(start)
        self._end = int(end)

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end


class _WorkspaceFiles:
    def __init__(
        self,
        *,
        conn: Any,
        root_path: str,
        include: str | None,
        exclude: str | None,
    ) -> None:
        self.conn = conn
        self.root_path = str(root_path or "")
        self.include_patterns = _split_patterns(include)
        self.exclude_patterns = (*DEFAULT_EXCLUDES, *_split_patterns(exclude))

    async def iter_paths(self, budget: _TraversalBudget) -> AsyncIterator[str]:
        stack = [""]
        while stack and not budget.truncated:
            current = stack.pop()
            try:
                listed = await self.conn.request_files_list(
                    root_path=self.root_path,
                    rel_path=current,
                    timeout_seconds=10.0,
                )
            except CompanionGrpcError as exc:
                companion_service.raise_file_error(exc)

            entries = sorted(
                list(getattr(listed, "entries", []) or []),
                key=lambda item: str(getattr(item, "rel_path", "") or ""),
            )
            for entry in entries:
                rel_path = _safe_rel_path(str(getattr(entry, "rel_path", "") or ""))
                if not rel_path:
                    continue
                if not budget.consume_path():
                    return
                kind = str(getattr(entry, "kind", "") or "").lower()
                if kind in {"dir", "directory", "folder"}:
                    if _is_excluded(rel_path, self.exclude_patterns, is_dir=True):
                        continue
                    stack.append(rel_path)
                    continue
                if kind and kind not in {"file", "regular"}:
                    continue
                if _is_excluded(rel_path, self.exclude_patterns, is_dir=False):
                    continue
                if self.include_patterns and not _matches_any(
                    rel_path, self.include_patterns
                ):
                    continue
                yield rel_path

    async def read_text(
        self, rel_path: str, budget: _TraversalBudget
    ) -> _FileItem | None:
        safe_path = _safe_rel_path(rel_path)
        if not safe_path or _looks_binary_path(safe_path):
            return None
        if not budget.consume_read():
            return None
        try:
            result = await self.conn.request_files_read(
                root_path=self.root_path,
                rel_path=safe_path,
                max_bytes=MAX_TEXT_FILE_BYTES,
            )
        except CompanionGrpcError:
            return None
        content = str(getattr(result, "content", "") or "")
        if _looks_binary_content(content):
            return None
        return _FileItem(
            path=safe_path,
            content=content,
            truncated=bool(getattr(result, "truncated", False)),
        )


async def search(
    grpc_server: Any,
    *,
    space_id: int,
    q: str = "",
    kind: str = "all",
    include: str | None = None,
    exclude: str | None = None,
    case_sensitive: bool = False,
    regex: bool = False,
    limit: int | None = None,
) -> dict:
    clean_query = _clean_query(q)
    clean_kind = _clean_search_kind(kind)
    clean_limit = _clean_limit(limit)
    if regex:
        raise CodeNavigationValidationError(
            "regex search is not supported by fallback backend"
        )
    matcher = _SearchMatcher(
        clean_query, case_sensitive=bool(case_sensitive), regex=False
    )
    workspace = _workspace(grpc_server, space_id, include=include, exclude=exclude)
    budget = _new_traversal_budget()
    collector = _ResultCollector(limit=clean_limit, results=[])

    async for path in workspace.iter_paths(budget):
        if clean_kind in {"all", "file"}:
            match = matcher.search(path)
            if match is not None and not collector.add(
                {
                    "kind": "file",
                    "path": path,
                    "line": 1,
                    "column": int(match.start()) + 1,
                    "name": PurePosixPath(path).name,
                    "preview": path,
                    "backend": SEARCH_BACKEND,
                }
            ):
                break

        needs_text = clean_kind in {"all", "text"}
        needs_symbol = clean_kind in {"all", "symbol"}
        if not needs_text and not needs_symbol:
            continue
        item = await workspace.read_text(path, budget)
        if budget.truncated:
            break
        if item is None:
            continue

        if needs_text:
            for result in _text_results(item, matcher, backend=SEARCH_BACKEND):
                if not collector.add(result):
                    break
            if collector.truncated:
                break

        if needs_symbol:
            for result in _symbol_results_for_file(
                item.path, item.content, backend=SYMBOL_BACKEND
            ):
                if (
                    clean_query
                    and matcher.search(str(result.get("name") or "")) is None
                ):
                    continue
                if not collector.add(result):
                    break
            if collector.truncated:
                break

    return {
        "ok": True,
        "query": clean_query,
        "kind": clean_kind,
        "backend": SEARCH_BACKEND,
        "truncated": collector.truncated or budget.truncated,
        "results": collector.results,
    }


async def symbols(
    grpc_server: Any,
    *,
    space_id: int,
    q: str = "",
    include: str | None = None,
    exclude: str | None = None,
    limit: int | None = None,
) -> dict:
    clean_query = _clean_query(q)
    clean_limit = _clean_limit(limit)
    workspace = _workspace(grpc_server, space_id, include=include, exclude=exclude)
    budget = _new_traversal_budget()
    collector = _ResultCollector(limit=clean_limit, results=[])

    async for path in workspace.iter_paths(budget):
        item = await workspace.read_text(path, budget)
        if budget.truncated:
            break
        if item is None:
            continue
        for result in _symbol_results_for_file(
            item.path, item.content, backend=SYMBOL_BACKEND
        ):
            if (
                clean_query
                and clean_query.lower() not in str(result.get("name") or "").lower()
            ):
                continue
            if not collector.add(result):
                break
        if collector.truncated:
            break

    return {
        "ok": True,
        "query": clean_query,
        "backend": SYMBOL_BACKEND,
        "truncated": collector.truncated or budget.truncated,
        "results": collector.results,
    }


async def definition(grpc_server: Any, *, space_id: int, payload: Any) -> dict:
    _clean_location_payload(payload)
    symbol_name = _clean_symbol(_payload_value(payload, "symbol"))
    workspace = _workspace(grpc_server, space_id, include=None, exclude=None)
    budget = _new_traversal_budget()
    collector = _ResultCollector(limit=DEFAULT_LIMIT, results=[])

    async for path in workspace.iter_paths(budget):
        item = await workspace.read_text(path, budget)
        if budget.truncated:
            break
        if item is None:
            continue
        for result in _symbol_results_for_file(
            item.path, item.content, backend=DEFINITION_BACKEND
        ):
            if str(result.get("name") or "") != symbol_name:
                continue
            if not collector.add(result):
                break
        if collector.truncated:
            break

    return {
        "ok": True,
        "symbol": symbol_name,
        "backend": DEFINITION_BACKEND,
        "truncated": collector.truncated or budget.truncated,
        "results": collector.results,
    }


async def references(grpc_server: Any, *, space_id: int, payload: Any) -> dict:
    _clean_location_payload(payload)
    symbol_name = _clean_symbol(_payload_value(payload, "symbol"))
    workspace = _workspace(grpc_server, space_id, include=None, exclude=None)
    budget = _new_traversal_budget()
    collector = _ResultCollector(limit=DEFAULT_LIMIT, results=[])
    matcher = _word_matcher(symbol_name)

    async for path in workspace.iter_paths(budget):
        item = await workspace.read_text(path, budget)
        if budget.truncated:
            break
        if item is None:
            continue
        for line_no, line in enumerate(item.content.splitlines(), start=1):
            for match in matcher.finditer(line):
                if not collector.add(
                    {
                        "kind": "reference",
                        "path": item.path,
                        "line": line_no,
                        "column": int(match.start()) + 1,
                        "preview": _preview(line),
                        "backend": REFERENCE_BACKEND,
                    }
                ):
                    break
            if collector.truncated:
                break
        if collector.truncated:
            break

    return {
        "ok": True,
        "symbol": symbol_name,
        "backend": REFERENCE_BACKEND,
        "truncated": collector.truncated or budget.truncated,
        "results": collector.results,
    }


def status(grpc_server: Any, *, space_id: int) -> dict:
    _workspace(grpc_server, space_id, include=None, exclude=None)
    return {
        "ok": True,
        "backend": SEARCH_BACKEND,
        "ripgrep_available": bool(shutil.which("rg")),
        "lsp_available": False,
        "active_language_servers": [],
        "fallback_mode": True,
    }


def _workspace(
    grpc_server: Any,
    space_id: int,
    *,
    include: str | None,
    exclude: str | None,
) -> _WorkspaceFiles:
    space, companion = companion_service.load_space_and_optional_companion(space_id)
    conn = companion_service.require_online(grpc_server, companion=companion)
    return _WorkspaceFiles(
        conn=conn,
        root_path=str(space.root_path or ""),
        include=include,
        exclude=exclude,
    )


def _new_traversal_budget() -> _TraversalBudget:
    return _TraversalBudget(
        max_visited_paths=MAX_WORKSPACE_VISITED_PATHS,
        max_read_files=MAX_WORKSPACE_READ_FILES,
    )


def _text_results(
    item: _FileItem, matcher: _SearchMatcher, *, backend: str
) -> Iterable[dict]:
    for line_no, line in enumerate(item.content.splitlines(), start=1):
        match = matcher.search(line)
        if match is None:
            continue
        yield {
            "kind": "text",
            "path": item.path,
            "line": line_no,
            "column": int(match.start()) + 1,
            "preview": _preview(line),
            "backend": backend,
        }


def _symbol_results_for_file(path: str, content: str, *, backend: str) -> list[dict]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return _python_symbols(path, content, backend=backend)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return _js_ts_symbols(path, content, backend=backend)
    return []


def _python_symbols(path: str, content: str, *, backend: str) -> list[dict]:
    patterns = (
        ("class", re.compile(r"^(?P<indent>\s*)class\s+(?P<name>[A-Za-z_]\w*)\b")),
        (
            "function",
            re.compile(
                r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\("
            ),
        ),
        (
            "variable",
            re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z_]\w*)\s*(?::[^=]+)?="),
        ),
    )
    return _regex_symbols(path, content, patterns, backend=backend)


def _js_ts_symbols(path: str, content: str, *, backend: str) -> list[dict]:
    patterns = (
        (
            "class",
            re.compile(
                r"^\s*(?:export\s+default\s+|export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b"
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
            ),
        ),
        (
            "variable",
            re.compile(
                r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\b"
            ),
        ),
        (
            "method",
            re.compile(
                r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"
            ),
        ),
    )
    return _regex_symbols(path, content, patterns, backend=backend)


def _regex_symbols(
    path: str,
    content: str,
    patterns: Iterable[tuple[str, re.Pattern[str]]],
    *,
    backend: str,
) -> list[dict]:
    results: list[dict] = []
    containers: list[tuple[int, str]] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        indent = len(line) - len(line.lstrip(" "))
        while containers and indent <= containers[-1][0]:
            containers.pop()
        for kind, pattern in patterns:
            match = pattern.search(line)
            if match is None:
                continue
            name = match.group("name")
            column = int(match.start("name")) + 1
            container = containers[-1][1] if containers else ""
            result = {
                "kind": kind,
                "name": name,
                "container": container,
                "path": path,
                "line": line_no,
                "column": column,
                "preview": _preview(line),
                "backend": backend,
            }
            results.append(result)
            if kind in {"class", "function", "method"}:
                containers.append((indent, name))
            break
    return results


def _clean_query(value: str | None) -> str:
    query = str(value or "")
    if len(query) > MAX_QUERY_LENGTH:
        raise CodeNavigationValidationError("query is too long (<=500)")
    return query


def _clean_symbol(value: str | None) -> str:
    symbol = str(value or "").strip()
    if not symbol:
        raise CodeNavigationValidationError("symbol is required")
    if len(symbol) > MAX_SYMBOL_LENGTH:
        raise CodeNavigationValidationError("symbol is too long (<=200)")
    return symbol


def _clean_limit(value: int | None) -> int:
    try:
        raw = int(value if value is not None else DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raw = DEFAULT_LIMIT
    return max(1, min(raw, MAX_LIMIT))


def _clean_search_kind(value: str | None) -> str:
    kind = str(value or "all").strip().lower()
    if kind not in SEARCH_KINDS:
        raise CodeNavigationValidationError("kind must be one of all,file,text,symbol")
    return kind


def _payload_value(payload: Any, key: str) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get(key) or "")


def _clean_location_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise CodeNavigationValidationError("invalid payload")
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise CodeNavigationValidationError("path is required")
    if _safe_rel_path(raw_path) != _normalize_input_rel_path(raw_path):
        raise CodeNavigationValidationError("invalid path")
    for key in ("line", "column"):
        if key not in payload:
            continue
        try:
            if int(payload.get(key) or 0) < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CodeNavigationValidationError(f"{key} must be positive") from exc


def _split_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        part.strip().replace("\\", "/").strip("/")
        for part in re.split(r"[,;\n]", str(value))
        if part.strip()
    )


def _safe_rel_path(value: str) -> str:
    raw = _normalize_input_rel_path(value)
    if not raw or raw.startswith("/") or raw == "..":
        return ""
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _normalize_input_rel_path(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _is_excluded(path: str, patterns: tuple[str, ...], *, is_dir: bool) -> bool:
    parts = tuple(part for part in path.split("/") if part)
    if any(part in DEFAULT_EXCLUDES for part in parts):
        return True
    return _matches_any(path, patterns) or (
        is_dir and _matches_any(parts[-1], patterns)
    )


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name
    for pattern in patterns:
        if not pattern:
            continue
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        if "/" not in pattern and pattern in path.split("/"):
            return True
    return False


def _looks_binary_path(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    if not suffix:
        return False
    return suffix not in TEXT_EXTENSIONS


def _looks_binary_content(content: str) -> bool:
    if "\x00" in content:
        return True
    if not content:
        return False
    sample = content[:2048]
    control_count = sum(
        1 for char in sample if ord(char) < 32 and char not in "\n\r\t\f\b"
    )
    return control_count > max(8, len(sample) // 20)


def _word_matcher(symbol_name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(symbol_name)}(?!\w)")


def _preview(line: str, *, max_len: int = 160) -> str:
    text = " ".join(str(line or "").strip().split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."
