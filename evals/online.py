"""
智能搜索助手 — 在线监控套件（online_monitoring）

对「生产运行态」的健康度做判定，分两个互补视角：

1. 本地自检（offline-safe）：加载 monitoring/ 下的 Prometheus 告警规则与抓取配置，
   静态断言 SLA 门槛是否声明了 95% 可用性、关键告警是否就位（错误率/延迟/服务宕机）。
2. 指标探测（probe，可选）：向运行中的后端 /api/metrics?format=availability 发起
   探测，解析多窗口可用性，判定是否满足 95% SLA 门禁。

设计：默认「无运行实例也通过本地自检」，只有当显式 --probe 或探测到可达的后端时
才升级为实测 SLA 判定——保证 CI（无服务）与生产巡检（有服务）都能用。

用法:
    python -m evals --kind online_monitoring          # 本地自检（不联网）
    python -m evals --kind online_monitoring --probe   # 探测本地后端实测 SLA
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from evals.core import (
    KIND_ONLINE,
    Assertion,
    CaseResult,
    GateResult,
    Suite,
    SuiteResult,
    PROJECT_ROOT,
    register,
)

MONITORING_DIR = PROJECT_ROOT / "monitoring"
PROMETHEUS_DIR = MONITORING_DIR / "prometheus"
GRAFANA_DIR = MONITORING_DIR / "grafana"

# 后端地址（可用环境变量覆盖，默认 Docker 内网/本地回环）
BACKEND_BASE = os.environ.get("EVAL_BACKEND_URL", "http://localhost:8000")


# ============================================================
# 1. 本地自检：监控栈配置完整性
# ============================================================
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _find_alerts_rules(alerts: dict) -> list[dict]:
    rules: list[dict] = []
    for group in alerts.get("groups", []):
        rules.extend(group.get("rules", []))
    return rules


def _rule_expr_contains(rules: list[dict], *needles: str) -> bool:
    for r in rules:
        expr = r.get("expr", "") or ""
        if all(n in expr for n in needles):
            return True
    return False


def _find_rule_by_alert(rules: list[dict], alert_name: str) -> Optional[dict]:
    """按 alert 名称精确定位规则（语义校验的前提：不能只靠子串碰运气）。"""
    for r in rules:
        if r.get("alert") == alert_name:
            return r
    return None


# PromQL 比较方向：`> 0.05` / `>= 0.95` / `== 0` 等
_COMPARISON_RE = re.compile(r"(>=|<=|==|!=|>|<)\s*([\d.]+)")


def _expr_threshold(rule: Optional[dict]) -> tuple[str, float]:
    """解析规则的比较运算符与阈值数值，返回 (op, value)；无则返回 ('', 0.0)。"""
    if not rule:
        return "", 0.0
    m = _COMPARISON_RE.search(rule.get("expr", "") or "")
    if not m:
        return "", 0.0
    op = m.group(1)
    try:
        val = float(m.group(2))
    except ValueError:
        return op, 0.0
    return op, val


def _check_alert(
    rules: list[dict],
    alert_name: str,
    required_op: str,
    expected_threshold: float,
    required_tokens: tuple[str, ...],
) -> tuple[bool, str]:
    """
    语义校验单条告警：规则必须存在、比较方向正确、阈值数值精确、关键 token 齐全。

    返回 (passed, 说明)。子串匹配无法验证方向（如 `>` 误写 `<`）或数值（如 0.05 误写 0.5），
    这里三者逐一核对。
    """
    rule = _find_rule_by_alert(rules, alert_name)
    if rule is None:
        return False, f"缺少告警 {alert_name}"
    expr = rule.get("expr", "") or ""
    missing = [t for t in required_tokens if t not in expr]
    if missing:
        return False, f"{alert_name} 表达式缺 {missing}"
    op, val = _expr_threshold(rule)
    if op != required_op:
        return False, f"{alert_name} 比较方向为 {op!r}，应为 {required_op!r}"
    if val != expected_threshold:
        return False, f"{alert_name} 阈值为 {val}，应为 {expected_threshold}"
    return True, f"{alert_name} 语义正确（{op} {val}）"


def _local_check() -> list[CaseResult]:
    cases: list[CaseResult] = []

    def _mk(case_id, name, category, cond, msg_ok="", msg_fail="") -> CaseResult:
        cr = CaseResult(case_id, name, category, passed=bool(cond))
        cr.assertions.append(Assertion(
            name="check", passed=bool(cond),
            message="" if cond else (msg_fail or "未满足"),
            expected=msg_ok or True, actual="通过" if cond else "未通过",
        ))
        return cr

    # ── Prometheus 抓取配置 ──
    prom = _load_yaml(PROMETHEUS_DIR / "prometheus.yml")
    scrape = prom.get("scrape_configs", [])
    backend_job = next((s for s in scrape if s.get("job_name") == "search-agent-backend"), None)
    cases.append(_mk(
        "mon-001", "Prometheus 后端抓取任务已配置", "config",
        backend_job is not None,
        "job=search-agent-backend 存在",
        "缺少 search-agent-backend 抓取任务",
    ))
    cases.append(_mk(
        "mon-002", "抓取指向 /api/metrics", "config",
        bool(backend_job and backend_job.get("metrics_path") == "/api/metrics"),
        "metrics_path=/api/metrics",
        "metrics_path 未指向 /api/metrics",
    ))

    # ── 告警规则（语义校验：方向 + 数值 + 关键 token）──
    alerts = _load_yaml(PROMETHEUS_DIR / "alerts.yml")
    rules = _find_alerts_rules(alerts)

    ok, detail = _check_alert(
        rules, "HighErrorRate", ">", 0.05,
        ("http_requests_total", "5..", "rate"),
    )
    cases.append(_mk("mon-003", "95% 可用性告警（5xx 错误率 > 5%）", "sla", ok, detail, detail))

    ok, detail = _check_alert(
        rules, "HighLatency", ">", 10.0,
        ("http_request_duration_seconds", "histogram_quantile", "0.95"),
    )
    cases.append(_mk("mon-004", "高延迟告警（P95 > 10s）", "sla", ok, detail, detail))

    ok, detail = _check_alert(
        rules, "ServiceDown", "==", 0.0,
        ('up{job="search-agent-backend"}',),
    )
    cases.append(_mk("mon-005", "服务宕机告警（up==0）", "sla", ok, detail, detail))

    # ── Grafana Dashboard ──
    dashboards = _load_yaml(GRAFANA_DIR / "dashboards" / "availability.json")
    panels = dashboards.get("panels", [])
    cases.append(_mk(
        "mon-006", "Grafana 可用性 Dashboard 含面板", "config",
        len(panels) > 0,
        f"{len(panels)} 个面板",
        "availability.json 无面板",
    ))

    return cases


# ============================================================
# 2. 指标探测：实测可用性（需运行中的后端）
# ============================================================
async def _probe_availability(timeout: float = 5.0) -> dict:
    """探测后端 /api/metrics?format=availability，返回多窗口可用性。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{BACKEND_BASE}/api/metrics?format=availability")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _probe_cases(data: dict) -> list[CaseResult]:
    cases: list[CaseResult] = []
    if "error" in data:
        cr = CaseResult("probe-000", "后端可达性", "probe", passed=False, error=data["error"])
        cr.assertions.append(Assertion("reachable", False, message=data["error"]))
        return [cr]

    # data 形如 {"1m": {...}, "5m": {...}, ...}
    windows = ["1m", "5m", "15m", "1h"]
    for w in windows:
        win = data.get(w)
        if not win:
            continue
        availability = win.get("availability_pct", 0.0)
        meets = win.get("meets_95_sla", False)
        passed = meets
        cr = CaseResult(f"probe-{w}", f"{w} 窗口可用性 SLA", "probe", passed=passed)
        cr.assertions.append(Assertion(
            name="meets_95_sla", passed=passed,
            message="" if passed else f"{w} 窗口可用性 {availability}% < 95%",
            expected=">= 95%", actual=f"{availability}%",
        ))
        cases.append(cr)

    # 至少有一个窗口被抓到才视为有效探测
    if not any(c.case_id != "probe-000" for c in cases):
        cr = CaseResult("probe-001", "可用性数据可用", "probe", passed=False,
                        error="后端返回的可用性数据不含任何窗口")
        cr.assertions.append(Assertion("windows_present", False, message="无窗口数据"))
        cases.append(cr)
    return cases


# ============================================================
# 套件
# ============================================================
@register
class OnlineMonitoringSuite(Suite):
    kind = KIND_ONLINE
    name = "在线监控（可用性 SLA 与监控栈）"
    probe: bool = False  # 是否探测运行中的后端

    async def run(self) -> SuiteResult:
        cases = _local_check()
        probe_cases: list[CaseResult] = []

        if self.probe:
            data = await _probe_availability()
            probe_cases = _probe_cases(data)
            cases = cases + probe_cases

        local_passed = all(c.passed and not c.error for c in cases if not c.case_id.startswith("probe"))
        probe_passed = all(c.passed and not c.error for c in probe_cases)
        passed = local_passed and probe_passed

        return SuiteResult(
            kind=self.kind,
            name=self.name,
            passed=passed,
            cases=cases,
            summary={
                "config_checks": sum(1 for c in cases if not c.case_id.startswith("probe")),
                "probe_windows": len(probe_cases),
                "probe_enabled": self.probe,
                "backend": BACKEND_BASE,
            },
            error=None if passed else "监控栈配置缺失或实测 SLA 未达标",
        )

    def gate(self, result: SuiteResult) -> GateResult:
        passed = result.passed
        probe_enabled = result.summary.get("probe_enabled", False)
        if not probe_enabled:
            return GateResult(
                kind=self.kind,
                name=self.name,
                passed=passed,
                threshold="监控栈配置完整（本地自检，无实测 SLA）",
                actual="自检通过" if passed else "配置缺失",
            )
        return GateResult(
            kind=self.kind,
            name=self.name,
            passed=passed,
            threshold="实测各窗口可用性 >= 95% 且监控栈配置完整",
            actual="实测 SLA 达标" if passed else "实测 SLA 未达标",
        )
