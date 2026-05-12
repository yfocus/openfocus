# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ...domains.memory import service as memory_service


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def create_router(*, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/memory")
    def memory_view(request: Request):
        memory_service.maintenance()
        mem_dir = memory_service.memory_dir()
        state = memory_service.load_state_unlocked()
        audit_files = memory_service.collect_file_items(
            memory_service.audit_root(), "**/*.md"
        )
        daily_files = memory_service.collect_file_items(
            memory_service.daily_root(), "*.md"
        )
        selected_tab = str(request.query_params.get("tab") or "audit").strip().lower()
        if selected_tab not in {"audit", "daily", "long_term"}:
            selected_tab = "audit"
        selected_audit = str(request.query_params.get("audit_file") or "").strip()
        selected_daily = str(request.query_params.get("daily_file") or "").strip()
        if not selected_audit and audit_files:
            selected_audit = str(audit_files[0].get("rel_path") or "")
        if not selected_daily and daily_files:
            selected_daily = str(daily_files[0].get("rel_path") or "")
        long_term_path = memory_service.long_term_path()
        long_term_memory = memory_service.read_text(long_term_path)
        if not long_term_memory:
            long_term_memory = _read_text(mem_dir / "user_memory.md")
        return templates.TemplateResponse(
            request,
            "memory.html",
            {
                "selected_tab": selected_tab,
                "audit_files": audit_files,
                "daily_files": daily_files,
                "selected_audit": selected_audit,
                "selected_daily": selected_daily,
                "audit_content": memory_service.read_selected_file(selected_audit),
                "daily_content": memory_service.read_selected_file(selected_daily),
                "long_term_memory": long_term_memory,
                "state": state,
            },
        )

    @router.post("/memory/audit/summary", include_in_schema=False)
    def memory_audit_summary() -> RedirectResponse:
        memory_service.force_audit_summary(dt.datetime.now(dt.timezone.utc))
        return RedirectResponse(url="/memory?tab=audit", status_code=303)

    @router.post("/memory/save", include_in_schema=False)
    def memory_save(
        long_term_memory: str = Form(""),
        user_memory: str = Form(""),
        user_card: str = Form(""),
    ) -> RedirectResponse:
        mem_dir = memory_service.memory_dir()
        if user_card:
            (mem_dir / "user_card.md").write_text(user_card or "", encoding="utf-8")
        content = (
            long_term_memory if str(long_term_memory or "").strip() else user_memory
        )
        memory_service.long_term_path().write_text(content or "", encoding="utf-8")
        if user_memory:
            (mem_dir / "user_memory.md").write_text(user_memory or "", encoding="utf-8")
        return RedirectResponse(url="/memory?tab=long_term", status_code=303)

    return router
