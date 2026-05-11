#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import json
import os
import urllib.request


def send_focus_report(
    *,
    server_url: str,
    agent: str,
    task_name: str,
    status: str,
    user_prompt: str = "",
    assistant_response: str = "",
    goal_id: int | None = None,
    task_public_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    url = server_url.rstrip("/") + "/api/skills/focus_report"
    payload = {
        "agent": agent,
        "task_name": task_name,
        "status": status,
        "goal_id": goal_id,
        "task_public_id": task_public_id,
        "user_prompt": user_prompt,
        "assistant_response": assistant_response,
        "metadata": metadata or {},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="focus_report")
    p.add_argument(
        "--server", default=os.environ.get("OPENFOCUS_SERVER", "http://127.0.0.1:8001")
    )
    p.add_argument("--agent", default=os.environ.get("OPENFOCUS_AGENT", "external"))
    p.add_argument("--task-name", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--goal-id", type=int)
    p.add_argument("--task-public-id")
    p.add_argument("--user-prompt", default="")
    p.add_argument("--assistant-response", default="")
    args = p.parse_args(argv)

    out = send_focus_report(
        server_url=args.server,
        agent=args.agent,
        task_name=args.task_name,
        status=args.status,
        goal_id=args.goal_id,
        task_public_id=args.task_public_id,
        user_prompt=args.user_prompt,
        assistant_response=args.assistant_response,
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
