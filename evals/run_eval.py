"""
黄金集 E2E 调试入口（兼容旧路径 python evals/run_eval.py）。

内部走 evals.golden_e2e.GoldenE2ESuite，与 python -m evals --kind golden_e2e
等价，但支持更多调试参数。

用法:
    python evals/run_eval.py
    python evals/run_eval.py --only-ids test_001,test_002
    python evals/run_eval.py --concurrency 1 --dataset evals/datasets/golden_dataset.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许直接 `python evals/run_eval.py`（不通过 -m）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.core import export_json, export_html  # noqa: E402
from evals.golden_e2e import (  # noqa: E402
    DEFAULT_CSV_PATH, DEFAULT_DATASET_PATH, GoldenE2ESuite,
)


async def _main() -> int:
    ap = argparse.ArgumentParser(description="黄金集 E2E 评测")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    ap.add_argument("--only-ids", default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--csv", default=str(DEFAULT_CSV_PATH))
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args()

    suite = GoldenE2ESuite()
    suite.dataset_path = Path(args.dataset)
    suite.csv_path = Path(args.csv)
    suite.concurrency = args.concurrency
    suite.enable_csv = not args.no_csv
    if args.only_ids:
        suite.only_ids = [x.strip() for x in args.only_ids.split(",") if x.strip()]

    result = await suite.run()
    gate = suite.gate(result)

    print("\n" + "=" * 68)
    print("  黄金集 E2E 结果")
    print("=" * 68)
    print(f"  {result.name}")
    print(f"  duration: {result.duration_ms:.0f}ms")
    for k, v in result.summary.items():
        print(f"  {k}: {v}")
    print(f"\n  门禁: [{'PASS' if gate.passed else 'FAIL'}] {gate.threshold}")
    print(f"        实际: {gate.actual}")
    if result.error:
        print(f"  error: {result.error}")

    if not result.passed:
        print("\n  失败用例:")
        for c in result.cases:
            if c.passed and not c.error:
                continue
            print(f"    - {c.case_id}  {c.name}")
            if c.error:
                print(f"        error: {c.error}")
            for a in c.assertions:
                if not a.passed:
                    print(f"        ✗ {a.name}: {a.message[:120]}")

    print("=" * 68 + "\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(asyncio.run(_main()))