# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import json

from .agents.attention_scheduler import AttentionSchedulerAgent
from .agents.task_decomposer import TaskDecomposerAgent
from .llm.openai_compat import OpenAICompatibleProvider
from .storage.events import DbEventSink


def _build_provider() -> OpenAICompatibleProvider:
    # 让 CLI 直接复用 env 配置
    return OpenAICompatibleProvider.from_env()


def cmd_decompose(args: argparse.Namespace) -> int:
    provider = _build_provider()
    sink = DbEventSink()
    agent = TaskDecomposerAgent(goal_id=args.goal_id, provider=provider)
    out = agent.run(sink=sink)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    provider = _build_provider()
    sink = DbEventSink()
    agent = AttentionSchedulerAgent(provider=provider, goal_id=args.goal_id)
    out = agent.run(sink=sink)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openfocus-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("decompose", help="把 goal 拆解为 tasks")
    p1.add_argument("--goal-id", type=int, required=True)
    p1.set_defaults(func=cmd_decompose)

    p2 = sub.add_parser("recommend", help="推荐下一步要做的 task")
    p2.add_argument("--goal-id", type=int, required=False)
    p2.set_defaults(func=cmd_recommend)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
