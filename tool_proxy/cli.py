"""Command line entry points for scenario runs and application/citation queries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .scenario import run_scenario
from .service import ToolProxy
from .store import JsonlEventStore


def _print_json(value: Any, compact: bool = False) -> None:
    if compact:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="工具代理：语义感知路由、预算、引用与恢复查询")
    sub = parser.add_subparsers(dest="command", required=True)

    scenario = sub.add_parser("scenario", help="运行 JSONL 演练并输出 inspect 结果")
    scenario.add_argument("path", type=Path)
    scenario.add_argument("--store", type=Path, default=None, help="可选的持久化事件目录")
    scenario.add_argument("--compact", action="store_true")

    query = sub.add_parser("query", help="从事件日志/JSONL 恢复并查询申请或引用")
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("--store", type=Path, help="持久化事件目录（events.jsonl）")
    source.add_argument("--events", type=Path, help="直接从 JSONL 事件/演练文件恢复")
    query.add_argument("--application", "--app", dest="application_id")
    query.add_argument("--citation", dest="citation_id")
    query.add_argument("--ref", dest="ref")
    query.add_argument("--compact", action="store_true")

    demo = sub.add_parser("demo", help="运行本地合成工具端到端演示")
    demo.add_argument("--compact", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "scenario":
        outcome = run_scenario(args.path, store_directory=args.store)
        output = {key: value for key, value in outcome.items() if key != "proxy"}
        _print_json(output, args.compact)
        return 0

    if args.command == "query":
        if args.store:
            proxy = ToolProxy.recover_from_store(JsonlEventStore(args.store))
        else:
            events = [
                json.loads(line)
                for line in args.events.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            proxy = ToolProxy.from_events(events)
        if args.citation_id:
            result = proxy.query_citation(args.citation_id)
        elif args.ref:
            result = proxy.query_citation(args.ref)
        elif args.application_id:
            result = proxy.query_application(args.application_id)
        else:
            result = {
                "found": False,
                "error": "provide --application, --citation or --ref",
                "application_ids": sorted(proxy.apps),
                "citation_ids": sorted(proxy.citations),
            }
        _print_json(result, args.compact)
        return 0 if result.get("found", True) else 2

    if args.command == "demo":
        from .demo import run_demo

        _print_json(run_demo(), args.compact)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
