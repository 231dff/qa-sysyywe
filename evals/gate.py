"""
门禁对比脚本（gate）：对比当前评测结果与基线，判定是否允许合入。

核心规则（与 METRICS.md 对齐）：
  1. 数据集一致性：baseline 与 current 的 case_id 集合必须完全一致（否则请重建基线）
  2. Error Rate <= 阈值（默认 10%，数据可信度前置）
  3. Pass@1 下降 <= 阈值（默认 5pp，剔除 time_sensitive 题）
  4. Veto count 不得增加（全量统计，time_sensitive 也管）
  5. Citation Validity 下降 <= 阈值（默认 10pp，剔除 time_sensitive 题）

退出码：
  0 = PASS（全部门禁通过）
  1 = FAIL（有门禁未通过）
  2 = ERROR（配置/IO 错误）

用法:
  # 首次或数据集变更后：把当前 CSV 存为基线
  python -m evals.gate --save-baseline

  # 对比当前 vs 基线
  python -m evals.gate

  # 收紧阈值 + 输出 JSON 供 CI 消费
  python -m evals.gate --pass-tolerance 3 --output-json evals/reports/gate_result.json

  # 直接跑现有 golden_e2e 再对比（一步到位）
  python -m evals --kind golden_e2e && python -m evals.gate
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = PROJECT_ROOT / "evals" / "reports" / "baseline" / "baseline_report.csv"
DEFAULT_CURRENT = PROJECT_ROOT / "evals" / "reports" / "eval_report.csv"
DEFAULT_DATASET = PROJECT_ROOT / "evals" / "datasets" / "golden_dataset.jsonl"


# ============================================================
# 颜色（无依赖 ANSI，Windows 自动开 VT）
# ============================================================
def _enable_ansi_on_windows() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004, stdout handle = -11
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class Color:
    enabled = True
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def _c(cls, code: str, s: str) -> str:
        return f"{code}{s}{cls.RESET}" if cls.enabled else s

    @classmethod
    def green(cls, s): return cls._c(cls.GREEN, s)
    @classmethod
    def red(cls, s): return cls._c(cls.RED, s)
    @classmethod
    def yellow(cls, s): return cls._c(cls.YELLOW, s)
    @classmethod
    def gray(cls, s): return cls._c(cls.GRAY, s)
    @classmethod
    def bold(cls, s): return cls._c(cls.BOLD, s)


# ============================================================
# 数据模型
# ============================================================
@dataclass
class CaseSnapshot:
    case_id: str
    category: str
    passed: bool
    veto: bool
    fact_score: int
    grounded_score: int
    citation_score: int
    error: str
    latency_ms: int


@dataclass
class Metrics:
    total: int = 0                  # 全量题数
    effective_total: int = 0        # 剔除 time_sensitive 后的分母（Pass@1 用）
    passed: int = 0                 # 非 error 且非 time_sensitive 且 passed
    error_count: int = 0            # 全量 error
    veto_count: int = 0             # 全量 veto
    citation_pass: int = 0
    citation_total: int = 0
    time_sensitive_excluded: int = 0

    @property
    def pass_at_1(self) -> float:
        return self.passed / self.effective_total if self.effective_total else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.total if self.total else 0.0

    @property
    def veto_rate(self) -> float:
        return self.veto_count / self.total if self.total else 0.0

    @property
    def citation_validity(self) -> float:
        return self.citation_pass / self.citation_total if self.citation_total else 0.0


@dataclass
class Diff:
    newly_passed: list[str] = field(default_factory=list)   # 基线失败 → 当前通过
    newly_failed: list[str] = field(default_factory=list)   # 基线通过 → 当前失败
    new_vetos: list[str] = field(default_factory=list)      # 基线无 veto → 当前有
    fixed_vetos: list[str] = field(default_factory=list)    # 基线有 veto → 当前无
    missing_in_current: list[str] = field(default_factory=list)
    new_in_current: list[str] = field(default_factory=list)

    @property
    def has_diff(self) -> bool:
        return bool(
            self.newly_passed or self.newly_failed or
            self.new_vetos or self.fixed_vetos or
            self.missing_in_current or self.new_in_current
        )


@dataclass
class GateCheck:
    name: str
    passed: bool
    threshold: str
    actual: str
    critical: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "threshold": self.threshold,
            "actual": self.actual,
            "critical": self.critical,
        }


@dataclass
class GateReport:
    baseline_path: str
    current_path: str
    baseline_mtime: str
    current_mtime: str
    baseline_metrics: Metrics
    current_metrics: Metrics
    diff: Diff
    checks: list[GateCheck]
    dataset_changed: bool = False
    passed: bool = True

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "baseline_path": self.baseline_path,
            "current_path": self.current_path,
            "baseline_mtime": self.baseline_mtime,
            "current_mtime": self.current_mtime,
            "dataset_changed": self.dataset_changed,
            "baseline_metrics": _metrics_to_dict(self.baseline_metrics),
            "current_metrics": _metrics_to_dict(self.current_metrics),
            "diff": {
                "newly_passed": self.diff.newly_passed,
                "newly_failed": self.diff.newly_failed,
                "new_vetos": self.diff.new_vetos,
                "fixed_vetos": self.diff.fixed_vetos,
                "missing_in_current": self.diff.missing_in_current,
                "new_in_current": self.diff.new_in_current,
            },
            "checks": [c.to_dict() for c in self.checks],
        }


def _metrics_to_dict(m: Metrics) -> dict:
    return {
        "total": m.total,
        "effective_total": m.effective_total,
        "passed": m.passed,
        "error_count": m.error_count,
        "veto_count": m.veto_count,
        "time_sensitive_excluded": m.time_sensitive_excluded,
        "pass_at_1": round(m.pass_at_1, 4),
        "error_rate": round(m.error_rate, 4),
        "veto_rate": round(m.veto_rate, 4),
        "citation_validity": round(m.citation_validity, 4),
    }


# ============================================================
# 加载
# ============================================================
def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "1", "yes")


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_report_csv(path: Path) -> dict[str, CaseSnapshot]:
    if not path.exists():
        raise FileNotFoundError(f"CSV 不存在: {path}")
    out: dict[str, CaseSnapshot] = {}
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid = (row.get("id") or "").strip()
            if not cid:
                continue
            out[cid] = CaseSnapshot(
                case_id=cid,
                category=(row.get("type") or "").strip(),
                passed=_to_bool(row.get("passed")),
                veto=_to_bool(row.get("veto")),
                fact_score=_to_int(row.get("fact_score")),
                grounded_score=_to_int(row.get("grounded_score")),
                citation_score=_to_int(row.get("citation_score")),
                error=(row.get("error") or "").strip(),
                latency_ms=_to_int(row.get("latency_ms")),
            )
    return out


def load_time_sensitive_ids(dataset_path: Optional[Path]) -> set[str]:
    """从 golden_dataset.jsonl 读 time_sensitive=true 的 id 集合。"""
    if not dataset_path or not dataset_path.exists():
        return set()
    ids: set[str] = set()
    with open(dataset_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("metadata", {}).get("time_sensitive"):
                cid = item.get("id")
                if cid:
                    ids.add(cid)
    return ids


def _file_mtime(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


# ============================================================
# 计算
# ============================================================
def compute_metrics(
    snapshots: dict[str, CaseSnapshot],
    time_sensitive_ids: set[str],
) -> Metrics:
    m = Metrics()
    for cid, c in snapshots.items():
        m.total += 1

        if c.error:
            m.error_count += 1
            continue

        if cid in time_sensitive_ids:
            m.time_sensitive_excluded += 1
        else:
            m.effective_total += 1
            if c.passed:
                m.passed += 1
            m.citation_total += 1
            if c.citation_score >= 3:
                m.citation_pass += 1

        if c.veto:
            m.veto_count += 1
    return m


def compute_diff(
    baseline: dict[str, CaseSnapshot],
    current: dict[str, CaseSnapshot],
) -> Diff:
    d = Diff()
    for cid, b in baseline.items():
        c = current.get(cid)
        if c is None:
            d.missing_in_current.append(cid)
            continue
        if not b.passed and c.passed:
            d.newly_passed.append(cid)
        elif b.passed and not c.passed:
            d.newly_failed.append(cid)
        if not b.veto and c.veto:
            d.new_vetos.append(cid)
        elif b.veto and not c.veto:
            d.fixed_vetos.append(cid)
    for cid in current:
        if cid not in baseline:
            d.new_in_current.append(cid)
    return d


def run_checks(
    bm: Metrics,
    cm: Metrics,
    diff: Diff,
    pass_tolerance: float,
    citation_tolerance: float,
    max_error_rate: float,
    dataset_changed: bool,
) -> list[GateCheck]:
    checks: list[GateCheck] = []

    # 1. 数据集一致性
    if dataset_changed:
        checks.append(GateCheck(
            name="数据集一致性",
            passed=False,
            threshold="baseline 与 current 的 case_id 集合必须完全一致",
            actual=(
                f"缺失 {len(diff.missing_in_current)} 题, "
                f"新增 {len(diff.new_in_current)} 题 —— 请重建基线"
            ),
            critical=True,
        ))
    else:
        checks.append(GateCheck(
            name="数据集一致性",
            passed=True,
            threshold="baseline 与 current 的 case_id 集合必须完全一致",
            actual=f"{bm.total} 题完全匹配",
            critical=True,
        ))

    # 2. Error rate（数据可信前置）
    checks.append(GateCheck(
        name="Error Rate",
        passed=cm.error_rate <= max_error_rate / 100,
        threshold=f"<= {max_error_rate:.1f}%",
        actual=f"{cm.error_rate * 100:.1f}% ({cm.error_count}/{cm.total})",
        critical=True,
    ))

    # 3. Pass@1 下降
    b_pass = bm.pass_at_1 * 100
    c_pass = cm.pass_at_1 * 100
    d_pass = c_pass - b_pass
    checks.append(GateCheck(
        name="Pass@1 下降幅度",
        passed=d_pass >= -pass_tolerance,
        threshold=(
            f"下降不超过 {pass_tolerance:.1f}pp "
            f"(基线 {b_pass:.1f}% → 最低 {b_pass - pass_tolerance:.1f}%)"
        ),
        actual=f"{c_pass:.1f}% (变化 {d_pass:+.1f}pp)",
        critical=True,
    ))

    # 4. Veto 增加
    checks.append(GateCheck(
        name="Veto Count",
        passed=cm.veto_count <= bm.veto_count,
        threshold=f"不超过基线 {bm.veto_count}",
        actual=f"{cm.veto_count} (变化 {cm.veto_count - bm.veto_count:+d})",
        critical=True,
    ))

    # 5. Citation validity 下降
    b_cit = bm.citation_validity * 100
    c_cit = cm.citation_validity * 100
    d_cit = c_cit - b_cit
    checks.append(GateCheck(
        name="Citation Validity 下降",
        passed=d_cit >= -citation_tolerance,
        threshold=(
            f"下降不超过 {citation_tolerance:.1f}pp "
            f"(基线 {b_cit:.1f}% → 最低 {b_cit - citation_tolerance:.1f}%)"
        ),
        actual=f"{c_cit:.1f}% (变化 {d_cit:+.1f}pp)",
        critical=True,
    ))

    return checks


# ============================================================
# 报告输出
# ============================================================
def print_report(r: GateReport) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  {Color.bold('门禁对比报告')}")
    print(sep)
    print(f"  基线: {r.baseline_path}  ({r.baseline_mtime})")
    print(f"  当前: {r.current_path}  ({r.current_mtime})")

    b, c = r.baseline_metrics, r.current_metrics

    print(f"\n  {Color.bold('核心指标')}")
    print(f"  {'指标':<22} {'基线':>10} {'当前':>10} {'变化':>12}")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*12}")

    def _row_pct(name: str, base: float, cur: float) -> str:
        delta = cur - base
        return f"  {name:<22} {base:>9.1f}% {cur:>9.1f}% {delta:>+11.1f}pp"

    def _row_int(name: str, base: int, cur: int) -> str:
        return f"  {name:<22} {base:>10d} {cur:>10d} {cur - base:>+12d}"

    print(_row_pct("Pass@1", b.pass_at_1 * 100, c.pass_at_1 * 100))
    print(_row_pct("Citation Validity", b.citation_validity * 100, c.citation_validity * 100))
    print(_row_pct("Error Rate", b.error_rate * 100, c.error_rate * 100))
    print(_row_int("Veto Count", b.veto_count, c.veto_count))
    print(_row_int("Total", b.total, c.total))
    print(_row_int("Time-sensitive(排除)", b.time_sensitive_excluded, c.time_sensitive_excluded))

    if r.diff.has_diff:
        print(f"\n  {Color.bold('逐题对比')}")
        if r.diff.newly_failed:
            print(f"  {Color.red('通过 → 失败')}: {len(r.diff.newly_failed)} 题")
            for cid in r.diff.newly_failed[:10]:
                print(f"    - {cid}")
            if len(r.diff.newly_failed) > 10:
                print(f"    ... 还有 {len(r.diff.newly_failed) - 10} 题")
        if r.diff.newly_passed:
            print(f"  {Color.green('失败 → 通过')}: {len(r.diff.newly_passed)} 题")
            for cid in r.diff.newly_passed[:10]:
                print(f"    + {cid}")
            if len(r.diff.newly_passed) > 10:
                print(f"    ... 还有 {len(r.diff.newly_passed) - 10} 题")
        if r.diff.new_vetos:
            print(f"  {Color.red('新增 Veto')}: {len(r.diff.new_vetos)} 题")
            for cid in r.diff.new_vetos:
                print(f"    - {cid}")
        if r.diff.fixed_vetos:
            print(f"  {Color.green('修复 Veto')}: {len(r.diff.fixed_vetos)} 题")
            for cid in r.diff.fixed_vetos:
                print(f"    + {cid}")
        if r.diff.missing_in_current:
            print(f"  {Color.yellow('当前缺失')}: {r.diff.missing_in_current}")
        if r.diff.new_in_current:
            print(f"  {Color.yellow('当前新增')}: {r.diff.new_in_current}")
    else:
        print(f"\n  {Color.gray('逐题无变化')}")

    print(f"\n  {Color.bold('门禁判定')}")
    for chk in r.checks:
        if chk.passed:
            mark = Color.green("[PASS]")
        elif chk.critical:
            mark = Color.red("[FAIL]")
        else:
            mark = Color.yellow("[WARN]")
        print(f"  {mark} {chk.name}")
        print(f"         阈值: {chk.threshold}")
        print(f"         实际: {chk.actual}")

    if r.passed:
        print(f"\n  result: {Color.green(Color.bold('PASS'))} 所有关键门禁通过")
    else:
        failed = sum(1 for x in r.checks if not x.passed and x.critical)
        print(f"\n  result: {Color.red(Color.bold('FAIL'))} {failed} 项关键门禁未通过")
    print(sep + "\n")


# ============================================================
# 主流程
# ============================================================
def save_baseline(current: Path, baseline: Path) -> int:
    baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, baseline)
    print(Color.green(f"[OK] 已保存基线: {baseline}"))
    print(f"     来源: {current}")
    print(f"     时间: {_file_mtime(baseline)}")
    return 0


def run_gate(args) -> int:
    baseline_path = Path(args.baseline)
    current_path = Path(args.current)
    dataset_path = Path(args.dataset) if args.dataset else None

    try:
        baseline = load_report_csv(baseline_path)
    except FileNotFoundError as e:
        print(Color.red(f"[ERROR] {e}"), file=sys.stderr)
        print("       首次运行请先执行: python -m evals.gate --save-baseline", file=sys.stderr)
        return 2

    try:
        current = load_report_csv(current_path)
    except FileNotFoundError as e:
        print(Color.red(f"[ERROR] {e}"), file=sys.stderr)
        print("       请先执行: python -m evals --kind golden_e2e", file=sys.stderr)
        return 2

    if not baseline or not current:
        print(Color.red("[ERROR] 基线或当前 CSV 为空"), file=sys.stderr)
        return 2

    ts_ids = load_time_sensitive_ids(dataset_path)

    bm = compute_metrics(baseline, ts_ids)
    cm = compute_metrics(current, ts_ids)
    diff = compute_diff(baseline, current)
    dataset_changed = bool(diff.missing_in_current or diff.new_in_current)

    checks = run_checks(
        bm, cm, diff,
        pass_tolerance=args.pass_tolerance,
        citation_tolerance=args.citation_tolerance,
        max_error_rate=args.max_error_rate,
        dataset_changed=dataset_changed,
    )

    report = GateReport(
        baseline_path=str(baseline_path),
        current_path=str(current_path),
        baseline_mtime=_file_mtime(baseline_path),
        current_mtime=_file_mtime(current_path),
        baseline_metrics=bm,
        current_metrics=cm,
        diff=diff,
        checks=checks,
        dataset_changed=dataset_changed,
        passed=all(c.passed for c in checks if c.critical),
    )

    print_report(report)

    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[json] {out}")

    return 0 if report.passed else 1


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if os.getenv("NO_COLOR"):
        Color.enabled = False
    if os.name == "nt":
        _enable_ansi_on_windows()

    ap = argparse.ArgumentParser(
        description="门禁对比脚本：对比当前评测结果与基线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python -m evals.gate --save-baseline
      把当前 eval_report.csv 存为基线（首次或数据集变更后执行）

  python -m evals.gate
      用默认路径对比

  python -m evals.gate --pass-tolerance 3 --output-json evals/reports/gate_result.json
      收紧 Pass@1 阈值为 3pp，并输出 JSON 供 CI 消费

  python -m evals --kind golden_e2e && python -m evals.gate
      先跑评测再门禁（CI 常用）
""",
    )
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help="基线 CSV 路径")
    ap.add_argument("--current", default=str(DEFAULT_CURRENT),
                    help="当前 CSV 路径")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET),
                    help="golden_dataset.jsonl 路径（识别 time_sensitive）")
    ap.add_argument("--pass-tolerance", type=float, default=5.0,
                    help="Pass@1 允许下降百分点（默认 5.0）")
    ap.add_argument("--citation-tolerance", type=float, default=10.0,
                    help="Citation validity 允许下降百分点（默认 10.0）")
    ap.add_argument("--max-error-rate", type=float, default=10.0,
                    help="Error rate 上限百分比（默认 10.0）")
    ap.add_argument("--save-baseline", action="store_true",
                    help="保存当前 CSV 为基线，不做对比")
    ap.add_argument("--output-json", default=None,
                    help="JSON 结果输出路径")
    ap.add_argument("--no-color", action="store_true",
                    help="禁用颜色（CI 环境用）")
    args = ap.parse_args()

    if args.no_color:
        Color.enabled = False

    if args.save_baseline:
        current = Path(args.current)
        if not current.exists():
            print(Color.red(f"[ERROR] 当前 CSV 不存在: {current}"), file=sys.stderr)
            return 2
        return save_baseline(current, Path(args.baseline))

    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())