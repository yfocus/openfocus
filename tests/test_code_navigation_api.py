# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from openfocus.companion.grpc import CompanionGrpcError
from openfocus.domains.code_navigation import service as code_navigation_service
from openfocus.web.routes import code_navigation as code_navigation_routes


class _Registry:
    def __init__(self, items: dict[int, Any] | None = None) -> None:
        self.items = {int(key): value for key, value in (items or {}).items()}

    def get(self, companion_id: int) -> Any:
        return self.items.get(int(companion_id))


class _GrpcServer:
    def __init__(self, items: dict[int, Any] | None = None) -> None:
        self.registry = _Registry(items)


class _FakeFilePort:
    def __init__(self, files: dict[str, str | bytes]) -> None:
        self.files = {_clean_rel(path): value for path, value in files.items()}
        self.read_calls: list[str] = []
        self.list_calls: list[str] = []

    async def request_files_list(
        self, *, root_path: str, rel_path: str, timeout_seconds: float = 10.0
    ) -> Any:
        rel = _clean_rel(rel_path)
        self.list_calls.append(rel)
        prefix = f"{rel}/" if rel else ""
        names: set[tuple[str, str]] = set()
        for path in self.files:
            if prefix and not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if not rest:
                continue
            head = rest.split("/", 1)[0]
            child = f"{prefix}{head}" if prefix else head
            kind = "directory" if "/" in rest else "file"
            names.add((child, kind))
        return SimpleNamespace(
            path=rel,
            entries=[
                SimpleNamespace(
                    name=PurePosixPath(child).name,
                    rel_path=child,
                    kind=kind,
                    size=len(self.files.get(child, b"")),
                    mtime=0.0,
                )
                for child, kind in sorted(names)
            ],
        )

    async def request_files_read(
        self, *, root_path: str, rel_path: str, max_bytes: int
    ) -> Any:
        rel = _clean_rel(rel_path)
        self.read_calls.append(rel)
        if rel not in self.files:
            raise CompanionGrpcError("not found")
        raw = self.files[rel]
        if isinstance(raw, bytes):
            content = raw.decode("utf-8", errors="replace")
            truncated = len(raw) > max_bytes
        else:
            encoded = raw.encode("utf-8")
            content = encoded[:max_bytes].decode("utf-8", errors="replace")
            truncated = len(encoded) > max_bytes
        return SimpleNamespace(
            path=rel,
            content=content,
            truncated=truncated,
            mime="text/plain",
        )


def _clean_rel(path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip("/")
    if raw.startswith("../") or "/../" in raw or raw == "..":
        raise CompanionGrpcError("path traversal rejected")
    if raw.startswith("/"):
        raise CompanionGrpcError("invalid path")
    return raw


def _group_by_path(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for result in results:
        group = by_path.get(result["path"])
        if group is None:
            group = {"path": result["path"], "results": []}
            by_path[result["path"]] = group
            groups.append(group)
        group["results"].append(result)
    return groups


def _app(grpc_server: _GrpcServer) -> FastAPI:
    app = FastAPI()
    app.include_router(code_navigation_routes.create_router(grpc_server=grpc_server))
    return app


def _create_bound_agent_space(tmp_path) -> tuple[int, int]:
    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, Companion

    with session_scope() as session:
        companion = Companion(
            device_id="code-navigation-device",
            name="code navigation companion",
            base_url="grpc://code-navigation",
            status="active",
            auth_token="token",
            last_seen_at=dt.datetime.now(dt.timezone.utc),
        )
        session.add(companion)
        session.flush()
        companion_id = int(companion.id)

        space = AgentSpace(
            task_public_id="code-navigation-task",
            companion_id=companion_id,
            root_path=str(tmp_path),
        )
        session.add(space)
        session.flush()
        return companion_id, int(space.id)


def test_code_search_returns_text_file_and_symbol_results(tmp_path) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort(
            {
                "app/focus_main.py": "class FocusRunner:\n    def run_focus(self):\n        return 'focus'\n",
                "web/focus_view.ts": "export function openFocus() {\n  return 'focus';\n}\n",
                "focus_notes.md": "focus guide\n",
            }
        )
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            response = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "focus", "kind": "all"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["query"] == "focus"
        assert payload["backend"] == "text_fallback"
        assert payload["truncated"] is False
        kinds = {item["kind"] for item in payload["results"]}
        assert {"file", "text", "function", "class"}.issubset(kinds)
        assert all(not item["path"].startswith("/") for item in payload["results"])
        assert any(item["path"] == "focus_notes.md" for item in payload["results"])
        assert any(item.get("name") == "openFocus" for item in payload["results"])
        assert payload["groups"] == _group_by_path(payload["results"])
        grouped_kinds = {
            group["path"]: {item["kind"] for item in group["results"]}
            for group in payload["groups"]
        }
        assert {"file", "text", "class"}.issubset(grouped_kinds["app/focus_main.py"])
        assert {"file", "text", "function"}.issubset(grouped_kinds["web/focus_view.ts"])

    asyncio.run(_run())


def test_code_symbols_definition_and_references_use_fallback_backends(
    tmp_path,
) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort(
            {
                "src/tool.py": (
                    "class FocusTool:\n"
                    "    def build_report(self):\n"
                    "        return build_report()\n"
                    "\n"
                    "def build_report():\n"
                    "    return 'ok'\n"
                ),
                "src/tool.test.py": "from src.tool import build_report\nresult = build_report()\n",
            }
        )
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            symbols = await client.get(
                f"/api/agent_spaces/{space_id}/code/symbols",
                params={"q": "build_report"},
            )
            definition = await client.post(
                f"/api/agent_spaces/{space_id}/code/definition",
                json={
                    "path": "src/tool.py",
                    "line": 3,
                    "column": 16,
                    "symbol": "build_report",
                },
            )
            references = await client.post(
                f"/api/agent_spaces/{space_id}/code/references",
                json={
                    "path": "src/tool.py",
                    "line": 3,
                    "column": 16,
                    "symbol": "build_report",
                },
            )

        assert symbols.status_code == 200
        assert symbols.json()["backend"] == "symbol_fallback"
        assert any(item["name"] == "build_report" for item in symbols.json()["results"])

        assert definition.status_code == 200
        definition_payload = definition.json()
        assert definition_payload["backend"] == "definition_fallback"
        assert any(
            item["path"] == "src/tool.py" and item["line"] == 5
            for item in definition_payload["results"]
        )

        assert references.status_code == 200
        reference_payload = references.json()
        assert reference_payload["backend"] == "reference_fallback"
        assert len(reference_payload["results"]) >= 4
        assert all(item["kind"] == "reference" for item in reference_payload["results"])

    asyncio.run(_run())


def test_code_search_respects_default_excludes_limit_and_query_validation(
    tmp_path,
) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort(
            {
                "src/a.py": "needle\n",
                "src/b.py": "needle\n",
                "node_modules/pkg/index.js": "needle\n",
                ".git/config": "needle\n",
            }
        )
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            limited = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "needle", "kind": "text", "limit": "1"},
            )
            too_long = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "x" * 501},
            )
            unsupported_regex = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "needle", "regex": "true"},
            )

        assert limited.status_code == 200
        payload = limited.json()
        assert payload["truncated"] is True
        assert len(payload["results"]) == 1
        assert payload["results"][0]["path"].startswith("src/")
        assert "node_modules" not in port.list_calls
        assert ".git" not in port.list_calls

        assert too_long.status_code == 400
        assert too_long.json()["detail"] == "query is too long (<=500)"
        assert unsupported_regex.status_code == 400
        assert (
            unsupported_regex.json()["detail"]
            == "regex search is not supported by fallback backend"
        )

    asyncio.run(_run())


def test_code_search_stops_traversal_when_result_limit_is_reached(tmp_path) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort(
            {
                "000_match.py": "needle\n",
                "zzz/one.py": "needle\n",
                "zzz/two.py": "needle\n",
            }
        )
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            response = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "needle", "kind": "text", "limit": "1"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["truncated"] is True
        assert [item["path"] for item in payload["results"]] == ["000_match.py"]
        assert port.read_calls == ["000_match.py"]
        assert port.list_calls == [""]

    asyncio.run(_run())


def test_code_navigation_reports_truncated_when_traversal_budget_is_exhausted(
    tmp_path,
    monkeypatch,
) -> None:
    async def _run() -> None:
        monkeypatch.setattr(code_navigation_service, "MAX_WORKSPACE_VISITED_PATHS", 2)
        monkeypatch.setattr(code_navigation_service, "MAX_WORKSPACE_READ_FILES", 10)
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort(
            {
                "src/a.py": "def alpha():\n    return 1\n",
                "src/b.py": "def beta():\n    return 2\n",
                "src/c.py": "def gamma():\n    return 3\n",
            }
        )
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        definition_payload = {
            "path": "src/a.py",
            "line": 1,
            "column": 5,
            "symbol": "missing_symbol",
        }
        async with client:
            search = await client.get(
                f"/api/agent_spaces/{space_id}/code/search",
                params={"q": "missing", "kind": "file"},
            )
            symbols = await client.get(
                f"/api/agent_spaces/{space_id}/code/symbols",
                params={"q": "missing"},
            )
            definition = await client.post(
                f"/api/agent_spaces/{space_id}/code/definition",
                json=definition_payload,
            )
            references = await client.post(
                f"/api/agent_spaces/{space_id}/code/references",
                json=definition_payload,
            )

        for response in (search, symbols, definition, references):
            assert response.status_code == 200
            assert response.json()["truncated"] is True

    asyncio.run(_run())


def test_code_definition_rejects_traversal_path(tmp_path) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort({"src/a.py": "def safe():\n    return safe()\n"})
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            response = await client.post(
                f"/api/agent_spaces/{space_id}/code/definition",
                json={
                    "path": "../secret.py",
                    "line": 1,
                    "column": 1,
                    "symbol": "safe",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "invalid path"
        assert port.read_calls == []

    asyncio.run(_run())


def test_code_status_reports_fallback_mode(tmp_path) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeFilePort({"src/a.py": "print('ok')\n"})
        client = AsyncClient(
            transport=ASGITransport(app=_app(_GrpcServer({companion_id: port}))),
            base_url="http://test",
        )
        async with client:
            response = await client.get(
                f"/api/agent_spaces/{space_id}/code/status",
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["backend"] == "text_fallback"
        assert payload["fallback_mode"] is True
        assert payload["lsp_available"] is False
        assert payload["active_language_servers"] == []

    asyncio.run(_run())
