"""
智能搜索助手 — 企业级评测核心框架

统一四类评测套件的公共底座：
  - 数据模型：Assertion / CaseResult / SuiteResult / Report / GateResult
  - Suite 注册表：按 kind 注册四类套件（offline_regression / online_monitoring /
    fault_injection / security_audit），支持按名/按类选择运行
  - 门禁（Gate）：每个套件声明自己的通过门槛，汇总为整体门禁结果
  - 报告导出：console / JSON / HTML 三种形态

设计原则：
  - 纯 Python、零新增依赖、Python 3.9 兼容
  - 所有套件只产出结构化结果，不直接决定退出码——退出码由门禁统一判定
  - 报告可机器消费（JSON），也可人读（console/HTML）
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 数据模型
# ============================================================
@dataclass
class Assertion:
    """单条断言结果。"""
    name: str
    passed: bool
    message: str = ""
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class CaseResult:
    """单个评测用例的结果。"""
    case_id: str
    name: str
    category: str
    passed: bool = True
    assertions: list[Assertion] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: float = 0.0
    # 观测项：不参与门禁与 pass_rate 统计，仅记录观测结果（如弱语义检索、probe）
    observational: bool = False

    @property
    def passed_assertions(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def total_assertions(self) -> int:
        return len(self.assertions)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "name": self.name,
            "category": self.category,
            "passed": self.passed,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "observational": self.observational,
            "assertions": [a.to_dict() for a in self.assertions],
        }


@dataclass
class SuiteResult:
    """单个评测套件的汇总结果。"""
    kind: str
    name: str
    passed: bool = True
    cases: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: float = 0.0

    @property
    def total_cases(self) -> int:
        return len(self.cases)

    @property
    def _gated_cases(self) -> list[CaseResult]:
        """参与门禁与 pass_rate 统计的用例（排除观测项）。"""
        return [c for c in self.cases if not c.observational]

    @property
    def passed_cases(self) -> int:
        return sum(1 for c in self._gated_cases if c.passed and not c.error)

    @property
    def failed_cases(self) -> int:
        return sum(1 for c in self._gated_cases if not c.passed and not c.error)

    @property
    def error_cases(self) -> int:
        return sum(1 for c in self._gated_cases if c.error)

    @property
    def pass_rate(self) -> float:
        gated = len(self._gated_cases)
        return self.passed_cases / gated if gated else 1.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "passed": self.passed,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "summary": self.summary,
            "stats": {
                "total_cases": self.total_cases,
                "observational": self.total_cases - len(self._gated_cases),
                "passed": self.passed_cases,
                "failed": self.failed_cases,
                "errors": self.error_cases,
                "pass_rate": round(self.pass_rate, 4),
            },
            "cases": [c.to_dict() for c in self.cases],
        }


@dataclass
class GateResult:
    """门禁判定结果：某套件的通过门槛是否满足。"""
    kind: str
    name: str
    passed: bool
    threshold: str  # 人类可读的阈值描述
    actual: str     # 人类可读的实际值描述

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "passed": self.passed,
            "threshold": self.threshold,
            "actual": self.actual,
        }


@dataclass
class Report:
    """一次完整评测运行的汇总报告。"""
    timestamp: str = ""
    suites: list[SuiteResult] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    overall_passed: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "overall_passed": self.overall_passed,
            "gates": [g.to_dict() for g in self.gates],
            "suites": [s.to_dict() for s in self.suites],
        }


# ============================================================
# Suite 基类
# ============================================================
KIND_OFFLINE = "offline_regression"
KIND_ONLINE = "online_monitoring"
KIND_FAULT = "fault_injection"
KIND_SECURITY = "security_audit"
KIND_QUALITY = "quality"
KIND_MEMORY = "memory"

# kind -> 中文名（报告展示用）
KIND_LABELS = {
    KIND_OFFLINE: "离线回归",
    KIND_ONLINE: "在线监控",
    KIND_FAULT: "故障注入",
    KIND_SECURITY: "安全审计",
    KIND_QUALITY: "检索质量",
    KIND_MEMORY: "记忆",
}


class Suite(ABC):
    """
    评测套件基类。

    子类实现 `run()`，返回 SuiteResult；可选实现 `gate()`，返回 GateResult。
    kind / name 用于注册表与门禁的标识与归类。
    """

    kind: str = ""
    name: str = ""

    @abstractmethod
    async def run(self) -> SuiteResult:
        """执行套件，返回结构化结果。"""

    def gate(self, result: SuiteResult) -> GateResult:
        """
        默认门禁：套件 passed 即通过。
        子类可按需覆盖为更细粒度的阈值判定（如安全审计要求 miss=0 且误报=0）。
        """
        return GateResult(
            kind=self.kind,
            name=self.name,
            passed=result.passed,
            threshold="suite.passed == True",
            actual=f"passed={result.passed}",
        )


# ============================================================
# 套件注册表
# ============================================================
_REGISTRY: dict[str, Suite] = {}


def register(suite) -> Suite:
    """注册一个套件（幂等：同名覆盖）。

    支持两种用法：
      - register(suite_instance)：直接注册实例
      - @register 装饰在 Suite 子类上：自动实例化后注册
    """
    if isinstance(suite, type):
        suite = suite()
    key = suite.kind
    _REGISTRY[key] = suite
    return suite


def get_suite(kind: str) -> Optional[Suite]:
    return _REGISTRY.get(kind)


def list_suites() -> list[Suite]:
    return list(_REGISTRY.values())


# ============================================================
# 报告导出
# ============================================================
def _json_safe(value: Any) -> Any:
    """把断言里的 expected/actual 兜底为可 JSON 序列化的值（set/自定义对象 → str）。"""
    if isinstance(value, (set, frozenset)):
        return sorted(list(value), key=str)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def export_json(report: Report, path: Optional[Path] = None) -> Path:
    path = path or PROJECT_ROOT / "evals" / "eval_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    # 断言中的 expected/actual 可能含 set/自定义对象，兜底为可序列化值
    for suite in payload.get("suites", []):
        for case in suite.get("cases", []):
            for a in case.get("assertions", []):
                a["expected"] = _json_safe(a.get("expected"))
                a["actual"] = _json_safe(a.get("actual"))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _html_escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def export_html(report: Report, path: Optional[Path] = None) -> Path:
    path = path or PROJECT_ROOT / "evals" / "eval_report.html"

    gate_rows = "".join(
        f'<tr><td>{_html_escape(KIND_LABELS.get(g.kind, g.kind))}</td>'
        f'<td>{_html_escape(g.name)}</td>'
        f'<td class="{"ok" if g.passed else "bad"}">{"PASS" if g.passed else "FAIL"}</td>'
        f'<td>{_html_escape(g.threshold)}</td>'
        f'<td>{_html_escape(g.actual)}</td></tr>'
        for g in report.gates
    )

    suite_blocks = ""
    for s in report.suites:
        s_rate = s.pass_rate * 100
        rows = ""
        for c in s.cases:
            if c.observational:
                status = "OBS" if c.passed else "OBS✗"
                cls = "obs"
            elif c.passed and not c.error:
                status, cls = "PASS", "ok"
            elif c.error:
                status, cls = "ERR", "bad"
            else:
                status, cls = "FAIL", "bad"
            fail_msgs = "<br>".join(
                _html_escape(a.message) for a in c.assertions if not a.passed
            )
            rows += (
                f'<tr><td><code>{_html_escape(c.case_id)}</code></td>'
                f'<td>{_html_escape(c.name)}</td>'
                f'<td>{_html_escape(c.category)}</td>'
                f'<td class="{cls}">{status}</td>'
                f'<td>{c.passed_assertions}/{c.total_assertions}</td>'
                f'<td>{_html_escape(c.error or "") or fail_msgs}</td></tr>'
            )
        suite_blocks += f"""
<div class="card">
  <h3>{_html_escape(KIND_LABELS.get(s.kind, s.kind))} — {_html_escape(s.name)}
      <span class="{"ok" if s.passed else "bad"}">{s.passed_cases}/{s.total_cases} 通过 ({s_rate:.0f}%)</span></h3>
  <table><tr><th>用例</th><th>名称</th><th>类别</th><th>结果</th><th>断言</th><th>详情</th></tr>{rows}</table>
</div>"""

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>企业级评测报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:system-ui,sans-serif;background:#f8fafc;padding:2rem;color:#0f172a}}
.container{{max-width:1100px;margin:0 auto}}h1{{font-size:1.5rem;margin-bottom:.25rem}}p.sub{{color:#64748b;margin-bottom:1.5rem}}
.card{{background:#fff;border-radius:12px;padding:1.25rem;margin-bottom:1.25rem;box-shadow:0 1px 3px #0001}}
.card h3{{margin-bottom:.75rem;font-size:1.05rem;display:flex;justify-content:space-between;align-items:center}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}th,td{{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #e2e8f0}}
th{{background:#f8fafc;font-weight:600}}tr:hover{{background:#f8fafc}}code{{font-family:ui-monospace,monospace;font-size:.8rem}}
.ok{{color:#16a34a;font-weight:600}}.bad{{color:#dc2626;font-weight:600}}.obs{{color:#64748b;font-weight:600}}
.badge{{display:inline-block;padding:.15rem .5rem;border-radius:999px;font-size:.75rem;font-weight:600}}
.badge.ok{{background:#dcfce7}}.badge.bad{{background:#fee2e2}}
.footer{{text-align:center;color:#94a3b8;font-size:.8rem;margin-top:1.5rem}}
</style></head><body><div class="container">
<h1>企业级评测报告</h1>
<p class="sub">{report.timestamp[:19].replace("T", " ")} · 整体判定:
<span class="badge {"ok" if report.overall_passed else "bad"}">{"ALL PASSED" if report.overall_passed else "HAS FAILURES"}</span></p>
<div class="card"><h3>门禁概览</h3>
<table><tr><th>类型</th><th>套件</th><th>门禁</th><th>阈值</th><th>实际</th></tr>{gate_rows}</table></div>
{suite_blocks}
<div class="footer">企业级评测 · 离线回归 + 在线监控 + 故障注入 + 安全审计</div>
</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


# ============================================================
# 统一运行器
# ============================================================
async def run_suites(
    kinds: Optional[list[str]] = None,
) -> Report:
    """按 kind 顺序运行套件，汇总为 Report（含门禁判定）。"""
    report = Report(timestamp=datetime.now(timezone.utc).isoformat())
    suites = (
        [s for s in _REGISTRY.values() if s.kind in kinds]
        if kinds
        else list(_REGISTRY.values())
    )
    # 固定顺序：离线回归 → 故障注入 → 安全审计 → 在线监控 → 检索质量 → 记忆
    order = {KIND_OFFLINE: 0, KIND_FAULT: 1, KIND_SECURITY: 2, KIND_ONLINE: 3, KIND_QUALITY: 4, KIND_MEMORY: 5}
    suites.sort(key=lambda s: order.get(s.kind, 99))
    for suite in suites:
        start = time.perf_counter()
        try:
            result = await suite.run()
        except Exception as exc:  # 套件级崩溃也要进入报告，不中断整个运行
            result = SuiteResult(
                kind=suite.kind,
                name=suite.name,
                passed=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        result.duration_ms = (time.perf_counter() - start) * 1000
        report.suites.append(result)

        try:
            gate = suite.gate(result)
        except Exception as exc:
            gate = GateResult(
                kind=suite.kind,
                name=suite.name,
                passed=False,
                threshold="gate()",
                actual=f"gate 计算异常: {exc}",
            )
        report.gates.append(gate)

    report.overall_passed = all(g.passed for g in report.gates)
    return report
