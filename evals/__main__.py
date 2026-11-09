"""
智能搜索助手 — 统一评测入口

用法:
    python -m evals                                # 跑全部套件
    python -m evals --kind offline_regression      # 单跑离线回归
    python -m evals --kind golden_e2e              # 单跑黄金集 E2E
    python -m evals --kind golden_e2e --only-ids test_001,test_002 --concurrency 2
    python -m evals --list                         # 列出已注册套件
    python -m evals -o report.json                 # 指定报告路径
    python -m evals --html                         # 额外输出 HTML
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from evals import core

# 导入套件以触发注册
from evals import (
    offline, fault_injection, security_audit, online,
    quality, memory_suite, golden_e2e,  # noqa: F401
)


def _print_console(report: core.Report) -> None:
    sep = "=" * 68
    print(f"\n{sep}")
    print("  企业级评测报告")
    print(f"  {report.timestamp[:19].replace('T', ' ')}")
    print(sep)

    print("\n  ── 门禁概览 ──")
    for g in report.gates:
        mark = "PASS" if g.passed else "FAIL"
        label = core.KIND_LABELS.get(g.kind, g.kind)
        print(f"  [{mark}] {label:<6s} {g.name}")
        print(f"        阈值: {g.threshold}")
        print(f"        实际: {g.actual}")

    print("\n  ── 套件明细 ──")
    for s in report.suites:
        mark = "PASS" if s.passed else "FAIL"
        label = core.KIND_LABELS.get(s.kind, s.kind)
        print(f"\n  [{mark}] {label} — {s.name}  ({s.duration_ms:.0f}ms)")
        if s.error:
            print(f"        ! {s.error}")
        for k, v in s.summary.items():
            print(f"        {k}: {v}")
        failures = [c for c in s.cases if not c.passed]
        for c in failures[:20]:
            status = "ERR" if c.error else ("OBS" if c.observational else "FAIL")
            print(f"        {status} {c.case_id}  {c.name[:40]}")
            if c.error:
                print(f"             {c.error[:100]}")
            for a in c.assertions:
                if not a.passed:
                    print(f"             ✗ {a.name}: {a.message[:80]}")

    overall = "ALL PASSED" if report.overall_passed else "HAS FAILURES"
    print(f"\n  result: {overall}")
    print(sep + "\n")


async def _main() -> int:
    ap = argparse.ArgumentParser(description="企业级评测统一入口")
    ap.add_argument("--kind", choices=[
        core.KIND_OFFLINE, core.KIND_ONLINE, core.KIND_FAULT,
        core.KIND_SECURITY, core.KIND_QUALITY, core.KIND_MEMORY,
        core.KIND_GOLDEN_E2E,
    ], default=None, help="仅运行指定类型的套件")
    ap.add_argument("--probe", action="store_true", help="在线监控：探测运行中的后端")
    ap.add_argument("--judge", action="store_true", help="检索质量：启用 LLM-as-judge")
    ap.add_argument("--list", action="store_true", help="列出已注册套件")
    ap.add_argument("-o", "--output", default=None, help="JSON 报告输出路径")
    ap.add_argument("--html", action="store_true", help="额外输出 HTML 报告")
    # golden_e2e 专用
    ap.add_argument("--only-ids", default=None,
                    help="仅跑指定 id，逗号分隔（例: test_001,test_002）")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="golden_e2e 并发数")
    ap.add_argument("--dataset", default=None,
                    help="golden_e2e 数据集路径")
    args = ap.parse_args()

    if args.list:
        print("\n已注册评测套件:")
        for s in core.list_suites():
            print(f"  - [{core.KIND_LABELS.get(s.kind, s.kind)}] {s.kind} :: {s.name}")
        print()
        return 0

    if args.probe:
        suite = core.get_suite(core.KIND_ONLINE)
        if suite is not None:
            suite.probe = True

    if args.judge:
        suite = core.get_suite(core.KIND_QUALITY)
        if suite is not None:
            suite.judge = True

    # golden_e2e 参数覆盖
    if args.only_ids or args.concurrency or args.dataset:
        suite = core.get_suite(core.KIND_GOLDEN_E2E)
        if suite is not None:
            if args.only_ids:
                suite.only_ids = [x.strip() for x in args.only_ids.split(",") if x.strip()]
            if args.concurrency:
                suite.concurrency = args.concurrency
            if args.dataset:
                suite.dataset_path = Path(args.dataset)

    kinds = [args.kind] if args.kind else None
    report = await core.run_suites(kinds)

    _print_console(report)

    json_path = Path(args.output) if args.output else None
    json_path = core.export_json(report, json_path)
    print(f"[json] {json_path}")
    if args.html:
        html_path = core.export_html(report)
        print(f"[html] {html_path}")

    return 0 if report.overall_passed else 1


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())