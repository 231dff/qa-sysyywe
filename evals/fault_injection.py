"""
智能搜索助手 — 故障注入套件（fault_injection）

系统化验证「生产级降级」：对每个外部依赖与持久化边界注入故障，断言组件
「优雅降级、绝不崩溃」——即返回安全默认值而非抛异常。

覆盖故障类别：
  - llm:      LLM 超时 / 5xx / 畸形 JSON / 错误结构 / 连接失败
  - search:   Tavily 超时 / 5xx / 畸形响应
  - storage:  知识图谱 / 知识库 JSON 文件损坏
  - pipeline: StructuredIndex 全链路抽取失败时仍完成 KB 写入

用法:
    python -m evals --kind fault_injection
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from evals.core import (
    KIND_FAULT,
    Assertion,
    CaseResult,
    Suite,
    SuiteResult,
    register,
)


# ============================================================
# 故障注入客户端工厂
# ============================================================
def _asynccm(m):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(timeout=30.0):
        yield m

    return _cm


class _LLMFault:
    """包装 rag.extractor.get_http_client，让 LLM 抽取命中指定故障。"""

    def __init__(self, fault: str):
        self.fault = fault

    def _resp(self):
        if self.fault == "timeout":
            raise httpx.TimeoutException("mock timeout")
        if self.fault == "5xx":
            r = MagicMock()
            r.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
            )
            return r
        if self.fault == "malformed_json":
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = {"choices": [{"message": {"content": "这不是 JSON"}}]}
            return r
        if self.fault == "wrong_shape":
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = {"choices": [{"message": {"content": '{"entities": "not-a-list"}'}}]}
            return r
        if self.fault == "connection_error":
            raise httpx.ConnectError("mock connection refused")
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = {"choices": [{"message": {"content": '{"entities": [], "relations": []}'}}]}
        return r

    def __enter__(self):
        m = MagicMock()
        m.post = AsyncMock(side_effect=lambda *a, **kw: self._resp())
        self._ctx = patch("rag.extractor.get_http_client", side_effect=_asynccm(m))
        return self._ctx.__enter__()

    def __exit__(self, *a):
        return self._ctx.__exit__(*a)


class _TavilyFault:
    """包装 tools.registry.get_http_client，让 tavily_search 命中指定故障。"""

    def __init__(self, fault: str):
        self.fault = fault

    def _resp(self):
        if self.fault == "timeout":
            raise httpx.TimeoutException("mock timeout")
        if self.fault == "5xx":
            r = MagicMock()
            r.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
            )
            return r
        if self.fault == "malformed_json":
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json.return_value = {"not": "results"}
            return r
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = {"results": []}
        return r

    def __enter__(self):
        m = MagicMock()
        m.post = AsyncMock(side_effect=lambda *a, **kw: self._resp())
        self._ctx = patch("tools.registry.get_http_client", side_effect=_asynccm(m))
        return self._ctx.__enter__()

    def __exit__(self, *a):
        return self._ctx.__exit__(*a)


# ============================================================
# 场景定义：每个场景是一个 (case_id, name, category, run, expected)
# ============================================================
async def _run_extract_with_fault(fault: str) -> dict:
    from rag.extractor import extract_entities_relations

    with _LLMFault(fault):
        out = await extract_entities_relations("张三的父亲是李四")
    return {
        "entities_is_list": isinstance(out.get("entities"), list),
        "relations_is_list": isinstance(out.get("relations"), list),
        "entities_empty": out.get("entities") == [],
        "relations_empty": out.get("relations") == [],
        "no_crash": True,
    }


async def _run_search_with_fault(fault: str) -> dict:
    from tools.registry import tavily_search

    with _TavilyFault(fault):
        r = await tavily_search("测试")
    return {
        "is_search_response": r.__class__.__name__ == "SearchResponse",
        "results_empty": r.results == [],
        "error_nonempty": bool(r.error),
        "no_crash": True,
    }


async def _run_graph_corrupt() -> dict:
    from rag.graph import KnowledgeGraph

    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / "g.json"
    p.write_text("{ 这不是合法 JSON !!", encoding="utf-8")
    kg = KnowledgeGraph(persist_dir=tmp.name, name="g")
    return {
        "entities_empty": kg.count_entities() == 0,
        "relations_empty": kg.count_relations() == 0,
        "no_crash": True,
    }


async def _run_kb_corrupt() -> dict:
    from rag import KnowledgeBase

    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / "k.json"
    p.write_text("[ 损坏的数据", encoding="utf-8")
    kb = KnowledgeBase(persist_dir=tmp.name, collection_name="k")
    return {
        "count_zero": kb.count() == 0,
        "no_crash": True,
    }


async def _run_pipeline_extract_fail() -> dict:
    from rag import KnowledgeBase
    from rag.graph import KnowledgeGraph
    from rag.structured import StructuredIndex

    tmp = tempfile.TemporaryDirectory()
    kb = KnowledgeBase(persist_dir=tmp.name, collection_name="k")
    kg = KnowledgeGraph(persist_dir=tmp.name, name="g")
    idx = StructuredIndex(kb=kb, kg=kg)

    with _LLMFault("timeout"):
        stats = await idx.index_documents(["张三的父亲是李四。"], source="fault")

    doc_id = kb._doc_id("张三的父亲是李四。", "fault")
    units = kb.units_of_doc(doc_id)
    entity_ids = units[0]["metadata"].get("entity_ids", []) if units else []
    return {
        "units_written": stats["units"] >= 1,
        "entities_zero": stats["entities"] == 0,
        "relations_zero": stats["relations"] == 0,
        "kb_has_doc": len(units) >= 1,
        "entity_ids_empty": entity_ids == [],
        "no_crash": True,
    }


# 场景列表：用 async 工厂闭包统一签名
def _scenarios() -> list[tuple[str, str, str, object, dict]]:
    return [
        ("llm-001", "LLM 超时 → 返回空实体/关系", "llm",
         lambda: _run_extract_with_fault("timeout"),
         {"no_crash": True, "entities_is_list": True, "relations_is_list": True,
          "entities_empty": True, "relations_empty": True}),
        ("llm-002", "LLM 5xx → 返回空", "llm",
         lambda: _run_extract_with_fault("5xx"),
         {"no_crash": True, "entities_empty": True, "relations_empty": True}),
        ("llm-003", "LLM 畸形 JSON → 返回空", "llm",
         lambda: _run_extract_with_fault("malformed_json"),
         {"no_crash": True, "entities_empty": True}),
        ("llm-004", "LLM 错误结构（entities 非 list）→ 返回空", "llm",
         lambda: _run_extract_with_fault("wrong_shape"),
         {"no_crash": True, "entities_is_list": True, "entities_empty": True}),
        ("llm-005", "LLM 连接失败 → 返回空", "llm",
         lambda: _run_extract_with_fault("connection_error"),
         {"no_crash": True, "entities_empty": True}),
        ("search-001", "Tavily 超时 → 空结果 + 错误信息", "search",
         lambda: _run_search_with_fault("timeout"),
         {"no_crash": True, "is_search_response": True, "results_empty": True, "error_nonempty": True}),
        ("search-002", "Tavily 5xx → 空结果 + 错误信息", "search",
         lambda: _run_search_with_fault("5xx"),
         {"no_crash": True, "results_empty": True, "error_nonempty": True}),
        ("search-003", "Tavily 畸形响应 → 空结果", "search",
         lambda: _run_search_with_fault("malformed_json"),
         {"no_crash": True, "results_empty": True}),
        ("storage-001", "图谱 JSON 损坏 → 加载空图不崩溃", "storage",
         _run_graph_corrupt,
         {"no_crash": True, "entities_empty": True, "relations_empty": True}),
        ("storage-002", "知识库 JSON 损坏 → 加载空库不崩溃", "storage",
         _run_kb_corrupt,
         {"no_crash": True, "count_zero": True}),
        ("pipeline-001", "抽取失败时 KB 仍写入，entity_ids 为空", "pipeline",
         _run_pipeline_extract_fail,
         {"no_crash": True, "units_written": True, "entities_zero": True,
          "relations_zero": True, "kb_has_doc": True, "entity_ids_empty": True}),
    ]


def _assert(name: str, expected, actual) -> Assertion:
    if isinstance(expected, bool):
        passed = bool(actual) == expected
        msg = "" if passed else f"期望 {expected}, 实际 {actual!r}"
    elif expected == "nonempty":
        passed = bool(actual)
        msg = "" if passed else f"期望非空, 实际 {actual!r}"
    elif expected == "empty":
        passed = not actual
        msg = "" if passed else f"期望空, 实际 {actual!r}"
    else:
        passed = actual == expected
        msg = "" if passed else f"期望 {expected!r}, 实际 {actual!r}"
    return Assertion(name=name, passed=passed, message=msg, expected=expected, actual=actual)


@register
class FaultInjectionSuite(Suite):
    kind = KIND_FAULT
    name = "故障注入（降级不崩溃）"

    async def run(self) -> SuiteResult:
        cases: list[CaseResult] = []
        for case_id, name, category, runner, expected in _scenarios():
            import time

            start = time.perf_counter()
            cr = CaseResult(case_id, name, category)
            try:
                facts = await runner()
                for key, exp in expected.items():
                    cr.assertions.append(_assert(key, exp, facts.get(key)))
                cr.passed = all(a.passed for a in cr.assertions)
            except Exception as exc:
                cr.passed = False
                cr.error = f"{type(exc).__name__}: {exc}"
            cr.duration_ms = (time.perf_counter() - start) * 1000
            cases.append(cr)

        passed = all(c.passed and not c.error for c in cases)
        return SuiteResult(
            kind=self.kind,
            name=self.name,
            passed=passed,
            cases=cases,
            summary={"scenarios": len(cases), "passed": sum(1 for c in cases if c.passed)},
            error=None if passed else "存在崩溃或未按预期降级的场景",
        )
