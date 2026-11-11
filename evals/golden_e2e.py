"""
智能搜索助手 — 黄金集黑盒 E2E 套件（golden_e2e）

与 offline.py 的区别：
- offline.py：白盒结构回归，mock 内部状态，验证降级/护栏/会话代码路径
- golden_e2e.py：黑盒端到端，真调 /api/chat/stream，验证「回答对不对」
                 用 LLM-as-Judge 评分（Day 2 的 evals/judge.py）

与 online.py 的一致约定：
- 后端地址从 EVAL_BACKEND_URL 环境变量读取，默认 http://localhost:8000

工作流程：
1. 加载 golden_dataset.jsonl（支持 --only-ids / 全量）
2. 并发调用 Agent 的 SSE 接口，收集 token / sources / progress
3. 并发调用 Judge.evaluate 对每条回答打分
4. 转为 SuiteResult + CaseResult（兼容现有报告体系）
5. 额外导出 CSV（Day 5 gate.py 可读）

用法:
    python -m evals --kind golden_e2e
    python -m evals --kind golden_e2e --only-ids test_001,test_002 --concurrency 2
    python evals/run_eval.py --dataset evals/datasets/golden_dataset.jsonl
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from evals.core import (
    KIND_GOLDEN_E2E,
    Assertion,
    CaseResult,
    GateResult,
    PROJECT_ROOT,
    Suite,
    SuiteResult,
    register,
)
from evals.judge import Judge, JudgeResult

# ============================================================
# 配置
# ============================================================
DEFAULT_DATASET_PATH = PROJECT_ROOT / "evals" / "datasets" / "golden_dataset.jsonl"
DEFAULT_CSV_PATH = PROJECT_ROOT / "evals" / "reports" / "eval_report.csv"
BACKEND_BASE = os.environ.get("EVAL_BACKEND_URL", "http://localhost:8000")
AGENT_PATH = "/api/chat/stream"

# 环境变量（如果都不传，走服务端 config.resolve_llm_connection）
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "") or os.getenv("JUDGE_API_KEY", "")
AGENT_API_BASE = (
    os.getenv("AGENT_API_BASE", "")
    or os.getenv("JUDGE_BASE_URL", "")
    or "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
AGENT_MODEL = os.getenv("AGENT_MODEL", "qwen-turbo")
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "180"))
DEFAULT_CONCURRENCY = int(os.getenv("EVAL_CONCURRENCY", "3"))


# ============================================================
# SSE 调用
# ============================================================
@dataclass
class AgentOutput:
    answer: str = ""
    sources: list[dict] = field(default_factory=list)
    progress_logs: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    error: str = ""
    latency_s: float = 0.0
    ttft_s: float = 0.0


def _build_payload(query: str, session_id: str) -> dict:
    payload: dict = {
        "query": query,
        "session_id": session_id,
        "user_id": "eval_bot",
        "enable_search": True,
        "enable_deep_think": True,
    }
    # 有独立 key 时显式传，否则走服务端 config
    if AGENT_API_KEY:
        payload["model"] = AGENT_MODEL
        payload["api_base"] = AGENT_API_BASE
        payload["api_key"] = AGENT_API_KEY
    return payload


async def _run_one_query(
    client: httpx.AsyncClient,
    query: str,
    session_id: str,
) -> AgentOutput:
    """对单轮 user query 发起 SSE 请求，收集回答。"""
    out = AgentOutput()
    t0 = time.perf_counter()
    url = f"{BACKEND_BASE}{AGENT_PATH}"
    payload = _build_payload(query, session_id)

    try:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            buffer = ""
            event_type = ""
            data_lines: list[str] = []
            async for chunk in resp.aiter_text():
                buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    event_type = ""
                    data_lines = []
                    for line in block.split("\n"):
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    if not (event_type and data_lines):
                        continue
                    try:
                        data = json.loads("\n".join(data_lines))
                    except Exception:
                        continue
                    if event_type == "token":
                        if out.ttft_s == 0.0:
                            out.ttft_s = time.perf_counter() - t0
                        out.answer += data.get("text", "")
                    elif event_type == "reset_tokens":
                        out.answer = ""
                    elif event_type == "progress":
                        msg = data.get("message", "")
                        if msg:
                            out.progress_logs.append(msg)
                    elif event_type == "sources":
                        for s in data.get("sources", []) or []:
                            if s not in out.sources:
                                out.sources.append(s)
                    elif event_type == "error":
                        out.error = data.get("message", "unknown")
                        out.error += f" (code={data.get('code', '')})"
    except httpx.HTTPError as e:
        out.error = f"HTTPError: {e}"
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"

    out.latency_s = time.perf_counter() - t0
    # 工具调用推断（与旧 run_eval 逻辑一致）
    if out.sources or any(("检索" in m) or ("search" in m.lower()) for m in out.progress_logs):
        out.tool_calls.append("search")
    return out


async def _run_agent_case(
    client: httpx.AsyncClient,
    item: dict,
) -> AgentOutput:
    """遍历多轮 user，session_id 保持一致（服务端靠 sid 维持 history）。"""
    sid = f"eval_{item['id']}_{int(time.time() * 1000)}"
    final = AgentOutput()
    for msg in item.get("messages", []):
        if msg.get("role") != "user":
            continue
        out = await _run_one_query(client, msg["content"], sid)
        # 最后一轮 user 的 output 作为本 case 的结果
        final = out
        if out.error:
            break
    return final


# ============================================================
# 断言
# ============================================================
def _tool_assertion(expected: list[str], actual: list[str]) -> Assertion:
    expected_set = set(expected or [])
    actual_set = set(actual or [])
    if not expected_set:
        passed = len(actual_set) == 0
        return Assertion(
            name="tools",
            passed=passed,
            message="" if passed else f"不期望调用工具，实际 {actual_set}",
            expected="[]",
            actual=sorted(actual_set),
        )
    missing = expected_set - actual_set
    passed = not missing
    return Assertion(
        name="tools",
        passed=passed,
        message="" if passed else f"漏调用 {sorted(missing)}",
        expected=sorted(expected_set),
        actual=sorted(actual_set),
    )


def _to_case_result(
    item: dict,
    agent_out: AgentOutput,
    judge_out: Optional[JudgeResult],
) -> CaseResult:
    cr = CaseResult(
        case_id=item["id"],
        name=item.get("messages", [{}])[-1].get("content", "")[:60],
        category=item.get("type", "unknown"),
        duration_ms=agent_out.latency_s * 1000,
    )

    # 1. Agent 本身失败 → error
    if agent_out.error:
        cr.passed = False
        cr.error = agent_out.error
        return cr

    # 2. Judge 失败 → error（Day 5 gate 会统计 error_count）
    if judge_out is None or judge_out.error:
        cr.passed = False
        cr.error = f"judge_error: {judge_out.error if judge_out else 'None'}"
        return cr

    # 3. 断言：tools 子集 + 4 条 Judge 断言
    cr.assertions.append(_tool_assertion(item.get("expected_tools"), agent_out.tool_calls))
    cr.assertions.extend(judge_out.to_assertions())

    cr.passed = all(a.passed for a in cr.assertions)
    return cr


# ============================================================
# 数据集加载
# ============================================================
def load_dataset(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ============================================================
# CSV 导出
# ============================================================
def _export_csv(suite_result: SuiteResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    def _assert_actual(case: CaseResult, name: str) -> str:
        for a in case.assertions:
            if a.name == name:
                return "" if a.actual is None else str(a.actual)
        return ""

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "type", "passed", "veto",
            "fact_score", "grounded_score", "citation_score",
            "error", "latency_ms",
            "reason",
        ])
        for c in suite_result.cases:
            # not_veto 断言实际存的是 veto_triggered（True 表示命中 veto）
            veto_actual = _assert_actual(c, "not_veto")
            veto_hit = veto_actual == "True"
            w.writerow([
                c.case_id,
                c.category,
                c.passed,
                veto_hit,
                _assert_actual(c, "fact_score>=3"),
                _assert_actual(c, "grounded_score>=3"),
                _assert_actual(c, "citation_score"),
                c.error or "",
                f"{c.duration_ms:.0f}",
                c.error or ("" if c.passed else "see assertions"),
            ])
    return path


# ============================================================
# 套件
# ============================================================
class GoldenE2ESuite(Suite):
    """
    黄金集黑盒 E2E。

    注意：本类不使用 @register 装饰器（装饰器会把类名替换为实例，
    导致外部无法 `GoldenE2ESuite()` 实例化传参）。改为模块末尾显式
    register(GoldenE2ESuite) 注册。
    """

    kind = KIND_GOLDEN_E2E
    name = "黄金集黑盒 E2E（真 Agent + LLM Judge）"

    def __init__(self):
        self.dataset_path: Path = DEFAULT_DATASET_PATH
        self.csv_path: Path = DEFAULT_CSV_PATH
        self.concurrency: int = DEFAULT_CONCURRENCY
        self.only_ids: list[str] = []
        self.enable_csv: bool = True

    async def run(self) -> SuiteResult:
        start = time.perf_counter()

        # 1. 加载数据集
        if not self.dataset_path.exists():
            return SuiteResult(
                kind=self.kind, name=self.name, passed=False,
                error=f"数据集不存在: {self.dataset_path}",
            )
        try:
            dataset = load_dataset(self.dataset_path)
        except Exception as e:
            return SuiteResult(
                kind=self.kind, name=self.name, passed=False,
                error=f"数据集加载失败: {e}",
            )

        # 2. 过滤 only_ids
        if self.only_ids:
            wanted = set(self.only_ids)
            dataset = [d for d in dataset if d["id"] in wanted]
            if not dataset:
                return SuiteResult(
                    kind=self.kind, name=self.name, passed=False,
                    error=f"only_ids 过滤后无数据: {self.only_ids}",
                )

        print(
            f"[golden_e2e] 开始: {len(dataset)} 题, "
            f"backend={BACKEND_BASE}, concurrency={self.concurrency}",
            file=sys.stderr,
        )

        # 3. 并发调用 Agent
        sem = asyncio.Semaphore(max(1, self.concurrency))

        async def _call(item: dict) -> tuple[dict, AgentOutput]:
            async with sem:
                return item, await _run_agent_case(client, item)

        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT, trust_env=False) as client:
            agent_results = await asyncio.gather(*[_call(d) for d in dataset])

        # 4. 并发调用 Judge
        try:
            judge = Judge()
        except RuntimeError as e:
            return SuiteResult(
                kind=self.kind, name=self.name, passed=False,
                error=f"Judge 初始化失败: {e}",
            )

        judge_cases = [
            {"item": item, "answer": out.answer, "sources": out.sources}
            for item, out in agent_results
            if not out.error
        ]
        judge_results: list[JudgeResult] = []
        try:
            if judge_cases:
                judge_results = await judge.evaluate_batch(
                    judge_cases, concurrency=4,
                )
        finally:
            await judge.close()

        # 5. 合并为 CaseResult（保持原 dataset 顺序）
        judge_by_id = {
            case["item"]["id"]: res
            for case, res in zip(judge_cases, judge_results)
        }
        case_results: list[CaseResult] = []
        for item, agent_out in agent_results:
            jr = judge_by_id.get(item["id"])
            case_results.append(_to_case_result(item, agent_out, jr))

        # 6. 汇总
        total = len(case_results)
        passed = sum(1 for c in case_results if c.passed and not c.error)
        error_count = sum(1 for c in case_results if c.error)
        veto_count = sum(
            1 for c in case_results
            for a in c.assertions
            if a.name == "not_veto" and a.actual is True
        )
        avg_latency = (
            sum(c.duration_ms for c in case_results) / total / 1000
            if total else 0.0
        )

        summary = {
            "total": total,
            "passed": passed,
            "failed": total - passed - error_count,
            "errors": error_count,
            "pass_at_1": round(passed / total, 4) if total else 0.0,
            "veto_rate": round(veto_count / total, 4) if total else 0.0,
            "avg_latency_s": round(avg_latency, 2),
        }

        suite_passed = error_count / total <= 0.10 if total else False

        result = SuiteResult(
            kind=self.kind,
            name=self.name,
            passed=suite_passed,
            cases=case_results,
            summary=summary,
            duration_ms=(time.perf_counter() - start) * 1000,
            error=(
                None if suite_passed
                else f"error_rate={error_count/total:.1%} 超过 10%，评测结果不可信"
            ),
        )

        # 7. CSV 导出
        if self.enable_csv:
            try:
                p = _export_csv(result, self.csv_path)
                print(f"[golden_e2e] CSV 已写入: {p}", file=sys.stderr)
            except Exception as e:
                print(f"[golden_e2e] CSV 导出失败: {e}", file=sys.stderr)

        return result

    def gate(self, result: SuiteResult) -> GateResult:
        # Day 3 阶段宽松：error_rate <= 10% 即视为「本次评测结果可信」
        # Day 5 会改为「Pass@1 对比基线」+「Veto 增加即 fail」
        total = result.summary.get("total", 0)
        errors = result.summary.get("errors", 0)
        error_rate = errors / total if total else 1.0
        pass_at_1 = result.summary.get("pass_at_1", 0.0)
        veto_rate = result.summary.get("veto_rate", 0.0)

        return GateResult(
            kind=self.kind,
            name=self.name,
            passed=result.passed,
            threshold="error_rate <= 10%（Day 5 收紧为 Pass@1 对比基线）",
            actual=(
                f"total={total} pass@1={pass_at_1:.1%} "
                f"veto_rate={veto_rate:.1%} error_rate={error_rate:.1%}"
            ),
        )


# ============================================================
# 显式注册（不用装饰器，保证 GoldenE2ESuite 保持为类）
# ============================================================
register(GoldenE2ESuite)


async def run_golden_e2e(**kwargs) -> SuiteResult:
    """外部代码可直接调用，支持传参覆盖。"""
    suite = GoldenE2ESuite()
    for k, v in kwargs.items():
        if hasattr(suite, k):
            setattr(suite, k, v)
    return await suite.run()