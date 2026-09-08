"""
智能搜索助手 — 检索与推理质量套件（quality）

弥补现有评测「只测结构、不测质量」的核心缺口，系统化验证：
1. RAG 检索命中率（离线确定性）——真实调用 KnowledgeBase.search，断言 top-k 命中期望来源
2. 知识图谱多跳推理（离线确定性）——真实调用 KnowledgeGraph，断言别名消歧与多跳路径正确
3. LLM-as-judge 忠实度（可选，默认跳过）——用 LLM 判「检索上下文是否支撑期望答案」

分层门禁设计：
- 强关键词检索 + 图谱推理 → 确定性，进门禁（回归即红，暴露真实 bug）
- 弱语义检索 → 仅观测（当前哈希特征对中文语义改写有已知弱点，进 summary 不进门禁）
- LLM-as-judge → 可选（依赖真实 API key，CI 离线跳过），结果仅观测不阻断

与现有代码风格一致：纯 Python、零新增依赖、离线可跑、失败兜底不抛异常。

用法:
    python -m evals --kind quality                 # 离线确定性（强检索 + 图谱）
    python -m evals --kind quality --judge         # 额外启用 LLM-as-judge（需 key）
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Optional

from evals.core import (
    KIND_QUALITY,
    Assertion,
    CaseResult,
    GateResult,
    Suite,
    SuiteResult,
    register,
)


# ============================================================
# 黄金语料：RAG 检索（自包含、确定性）
# ============================================================
# 固定制度文档，检索评测时灌入独立 in-memory KB（可重复、不依赖持久化状态）
_RAG_DOCS: list[tuple[str, str]] = [
    ("公司报销流程：员工需在每月25日前提交上月报销单据。报销单需附发票原件，经直属主管审批后交财务部。", "制度-报销"),
    ("项目部署规范：生产环境变更需代码评审，须经至少一名资深工程师批准后方可上线。", "制度-部署"),
    ("公司考勤制度：工作日 9:00-18:00，午休 12:00-13:00。迟到超过30分钟按事假半天计。", "制度-考勤"),
    ("招聘流程：技术岗位需两轮技术面试加一轮 HR 面试，最终 offer 由用人部门负责人审批。", "制度-招聘"),
    ("信息安全规范：员工不得将生产数据导出到个人设备，密钥须存入公司密码管理器。", "制度-安全"),
]

# 强关键词查询：与原文词面高度重合，是检索基本功，要求 hit@1（进门禁）
_RAG_STRONG: list[tuple[str, str]] = [
    ("报销需要什么材料", "制度-报销"),
    ("代码评审", "制度-部署"),
    ("几点下班", "制度-考勤"),
    ("密钥应该存在哪里", "制度-安全"),
    ("技术岗面试有几轮", "制度-招聘"),
]

# 弱语义查询：不含原文关键词，需要语义理解，仅观测 hit@3（不进门禁）
_RAG_WEAK: list[tuple[str, str]] = [
    ("我出差花了钱怎么要回来", "制度-报销"),
    ("改线上系统要走什么流程", "制度-部署"),
    ("想招一个工程师", "制度-招聘"),
    ("密码放哪里安全", "制度-安全"),
    ("谁批准代码改动", "制度-部署"),
]


def _build_kb():
    from rag import KnowledgeBase

    kb = KnowledgeBase(collection_name="quality_golden", in_memory=True)
    docs = [d for d, _ in _RAG_DOCS]
    metas = [{"source": s} for _, s in _RAG_DOCS]
    kb.add_documents(docs, metadatas=metas)
    return kb


# ============================================================
# RAG 检索命中率
# ============================================================
def _run_rag() -> list[CaseResult]:
    """真实调用 KnowledgeBase.search，断言命中期望来源。"""
    kb = _build_kb()
    cases: list[CaseResult] = []

    def _case(case_id: str, name: str, query: str, expected: str, require_top1: bool, observational: bool = False) -> CaseResult:
        start = time.perf_counter()
        cr = CaseResult(case_id, name, "rag_retrieval", observational=observational)
        try:
            top = kb.search(query, top_k=3)
            hit_top1 = bool(top and top[0].get("metadata", {}).get("source") == expected)
            hit_top3 = any(r.get("metadata", {}).get("source") == expected for r in top)
            passed = hit_top1 if require_top1 else hit_top3
            cr.passed = passed
            cr.assertions.append(Assertion(
                "hit_at_1" if require_top1 else "hit_at_3",
                passed,
                message="" if passed else f"检索未命中期望来源 {expected}",
                expected=expected,
                actual=[r.get("metadata", {}).get("source") for r in top],
            ))
        except Exception as exc:
            cr.passed = False
            cr.error = f"{type(exc).__name__}: {exc}"
        cr.duration_ms = (time.perf_counter() - start) * 1000
        return cr

    for i, (q, exp) in enumerate(_RAG_STRONG, 1):
        cases.append(_case(f"rag-strong-{i:02d}", f"强关键词检索命中「{q}」", q, exp, require_top1=True))
    for i, (q, exp) in enumerate(_RAG_WEAK, 1):
        cases.append(_case(f"rag-weak-{i:02d}", f"弱语义检索命中「{q}」", q, exp, require_top1=False, observational=True))
    return cases


# ============================================================
# 知识图谱多跳推理
# ============================================================
def _run_graph() -> list[CaseResult]:
    """真实调用 KnowledgeGraph，断言别名消歧与多跳路径正确。"""
    from rag.graph import KnowledgeGraph

    cases: list[CaseResult] = []

    def _case(case_id: str, name: str, cond: bool, msg_fail: str = "", **assertion) -> CaseResult:
        cr = CaseResult(case_id, name, "graph_reasoning", passed=bool(cond))
        cr.assertions.append(Assertion(
            "check", bool(cond), message="" if cond else msg_fail, **assertion,
        ))
        return cr

    with tempfile.TemporaryDirectory() as d:
        kg = KnowledgeGraph(persist_dir=d, name="quality")
        kg.add_entities([
            {"name": "张三", "type": "person", "aliases": ["三哥"]},
            {"name": "李四", "type": "person"},
            {"name": "王五", "type": "person"},
            {"name": "赵敏", "type": "person"},
        ])
        kg.add_relations([
            {"source": "张三", "target": "李四", "type": "father_of"},
            {"source": "李四", "target": "王五", "type": "father_of"},
            {"source": "张三", "target": "赵敏", "type": "spouse_of"},
            {"source": "赵敏", "target": "李四", "type": "mother_of"},
        ])

        # 1. 别名消歧：resolve_entity("三哥") 应解析到「张三」
        hit = kg.resolve_entity("三哥")
        cases.append(_case(
            "graph-001", "别名「三哥」消歧到「张三」",
            bool(hit and hit.get("name") == "张三"),
            msg_fail="别名未能解析到张三",
            expected="张三", actual=(hit or {}).get("name"),
        ))

        # 2. 多跳路径：张三 → 李四 → 王五，2 跳可达
        paths = kg.query_paths(["张三", "王五"], max_hops=2)
        has_2hop = any(p.get("hops") == 2 for p in paths)
        cases.append(_case(
            "graph-002", "「张三→王五」存在 2 跳路径",
            has_2hop,
            msg_fail="多跳推理未找到 2 跳路径",
            expected="hops==2 存在", actual=f"{len(paths)} 条路径",
        ))

        # 3. 限跳生效：max_hops=1 时不应出现 2 跳路径
        paths1 = kg.query_paths(["张三", "王五"], max_hops=1)
        no_2hop = not any(p.get("hops") >= 2 for p in paths1)
        cases.append(_case(
            "graph-003", "限跳 max_hops=1 生效（无 >=2 跳）",
            no_2hop,
            msg_fail="max_hops=1 仍返回了 2 跳路径",
            expected="无 >=2 跳", actual=f"{len(paths1)} 条路径",
        ))

        # 4. 双向关系：配偶关系从「赵敏」侧也能回溯到「张三」
        paths_spouse = kg.query_paths(["赵敏", "张三"], max_hops=1)
        has_spouse = any(
            any(s.get("relation") == "spouse_of" for s in p.get("path", []))
            for p in paths_spouse
        )
        cases.append(_case(
            "graph-004", "配偶关系双向可达",
            has_spouse,
            msg_fail="spouse_of 关系未能在赵敏→张三间找到",
            expected="spouse_of 可达", actual=f"{len(paths_spouse)} 条路径",
        ))

    return cases


# ============================================================
# LLM-as-judge 忠实度（可选，默认跳过）
# ============================================================
_JUDGE_SYSTEM_PROMPT = """你是检索质量的严格裁判。给定一个用户问题与检索到的知识片段，判断这些片段是否**足以支撑一个正确回答**。

## 输出格式（严格遵守 JSON）
{"supported": true, "reason": "简短理由"}

规则：
1. 片段包含回答问题所需的全部关键信息 → supported=true
2. 片段缺失关键信息或答非所问 → supported=false
3. 只基于片段内容判断，不要臆测"""


async def _llm_judge(question: str, doc_texts: list[str]) -> Optional[bool]:
    """用 LLM 判「检索片段是否支撑回答」。失败返回 None（由调用方降级为跳过）。"""
    from config import get_api_config, get_agent_config
    from utils.http_client import get_http_client

    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    docs_block = "\n\n---\n\n".join(f"[{i}] {t}" for i, t in enumerate(doc_texts, 1))
    user_message = f"## 用户问题\n{question}\n\n## 检索到的知识片段\n{docs_block}\n\n请输出 JSON:"

    request_body = {
        "model": api_cfg.llm_model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }
    if api_cfg.supports_response_format:
        request_body["response_format"] = {"type": "json_object"}

    try:
        async with get_http_client(timeout=30.0) as client:
            resp = await client.post(
                f"{api_cfg.llm_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_cfg.llm_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return bool(parsed.get("supported", False))
    except Exception as exc:
        if agent_cfg.verbose:
            print(f"[质量 judge 失败] {exc}, 跳过该条")
        return None


async def _run_judge() -> dict:
    """对强+弱 RAG 查询各跑一次 LLM-as-judge，返回观测统计（失败即降级跳过）。"""
    kb = _build_kb()
    total = supported = failed = 0
    for q, _exp in _RAG_STRONG + _RAG_WEAK:
        top = kb.search(q, top_k=3)
        verdict = await _llm_judge(q, [r["text"] for r in top])
        if verdict is None:
            failed += 1
            continue
        total += 1
        supported += int(verdict)
    return {"judged": total, "supported": supported, "failed": failed}


# ============================================================
# 套件
# ============================================================
@register
class QualitySuite(Suite):
    kind = KIND_QUALITY
    name = "检索与推理质量（RAG 命中率 + 图谱多跳 + 可选 judge）"
    judge: bool = False  # 是否启用 LLM-as-judge（需真实 API key）

    async def run(self) -> SuiteResult:
        rag_cases = _run_rag()
        graph_cases = _run_graph()
        cases = rag_cases + graph_cases

        judge_stats: dict = {}
        if self.judge:
            judge_stats = await _run_judge()

        # 门禁仅覆盖「强关键词检索 + 图谱推理」：确定性，回归即红
        strong = [c for c in rag_cases if c.case_id.startswith("rag-strong")]
        weak = [c for c in rag_cases if c.case_id.startswith("rag-weak")]
        strong_passed = sum(1 for c in strong if c.passed and not c.error)
        weak_hit3 = sum(1 for c in weak if c.passed and not c.error)
        graph_passed = sum(1 for c in graph_cases if c.passed and not c.error)

        passed = strong_passed == len(strong) and graph_passed == len(graph_cases)

        return SuiteResult(
            kind=self.kind,
            name=self.name,
            passed=passed,
            cases=cases,
            summary={
                "rag_strong_hit1": f"{strong_passed}/{len(strong)}",
                "rag_weak_hit3": f"{weak_hit3}/{len(weak)}",  # 仅观测
                "graph_passed": f"{graph_passed}/{len(graph_cases)}",
                "judge": judge_stats,
            },
            error=None if passed else "强关键词检索或图谱推理存在失败",
        )

    def gate(self, result: SuiteResult) -> GateResult:
        strong = result.summary.get("rag_strong_hit1", "?")
        graph = result.summary.get("graph_passed", "?")
        return GateResult(
            kind=self.kind,
            name=self.name,
            passed=result.passed,
            threshold="强关键词检索 hit@1 = 100% 且图谱推理全过",
            actual=f"强检索={strong}; 图谱={graph}",
        )
