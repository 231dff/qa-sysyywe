"""
智能搜索助手 — ReAct 工具集 (极速版)
"""
from __future__ import annotations

import os
os.environ["LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"] = "0"

import json
import time  # ★ 新增：用于计算耗时
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool
from tools.registry import search_and_filter_pipeline
from tools.web_extractor import fetch_and_extract
from utils.http_client import get_http_client
from utils.llm_context import get_llm_context, set_llm_context
from utils.metrics import record_search, record_graph_search  # ★ 新增：指标打点函数
from config import get_api_config

_current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")
_tool_call_counts: ContextVar[dict] = ContextVar("tool_call_counts", default={})
_recent_search_queries: ContextVar[list] = ContextVar("recent_search_queries", default=[])
_agent_depth: ContextVar[int] = ContextVar("agent_depth", default=0)

def set_current_user_id(user_id: str) -> None:
    _current_user_id.set(user_id or "")

def get_current_user_id() -> str:
    return _current_user_id.get()

def reset_tool_counts() -> None:
    _tool_call_counts.set({})
    _recent_search_queries.set([])
    _agent_depth.set(0)

def _increment_and_get_status(tool_name: str) -> str:
    counts = _tool_call_counts.get()
    counts[tool_name] = counts.get(tool_name, 0) + 1
    _tool_call_counts.set(counts)
    stats_str = ", ".join([f"{k}: {v} 次" for k, v in counts.items()])
    total_calls = sum(counts.values())
    warning = " ⚠️ 工具调用过多，请立刻作答！" if total_calls >= 3 else ""
    return f"\n\n<agent_status>[状态] 工具调用 -> {stats_str}。{warning}</agent_status>"


@tool
async def spawn_search_worker(query: str) -> str:
    """委派检索任务。极速模式下，直接将其降级为单次快速搜索，避免耗时。"""
    # 为了防止套娃，直接调用底层快速搜索，不再单独起一个 LangGraph
    return await search.ainvoke({"query": query})


@tool
async def read_tool_output(file_path: str) -> str:
    """读取因过长被截断落盘的文件。"""
    if not file_path.startswith("/tmp/tool_outputs/"):
        return "Error: 安全限制，只能读取工具输出目录下的文件。"
    if not os.path.exists(file_path):
        return f"Error: 文件 {file_path} 不存在或已被清理。"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            return content + _increment_and_get_status("read_tool_output")
    except Exception as e:
        return f"Error: 读取文件失败: {str(e)}"


@tool
async def fetch_webpage_content(url: str) -> str:
    """深度阅读指定网页正文。极速版默认流水线不抓网页，但保留此接口供模型在摘要不足时使用。"""
    try:
        async with get_http_client(timeout=10.0) as client:
            content = await fetch_and_extract(url, client)
            if not content:
                return "（该网页内容无法提取或为空）"
            max_chars = 8000
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n...（内容过长，已截断）..."
            return f"<webpage_content url='{url}'>\n{content}\n</webpage_content>" + _increment_and_get_status("fetch_webpage")
    except Exception as e:
        return f"读取网页失败: {str(e)}"


@tool
async def search(query: str, search_depth: str = "advanced", max_results: int = 5) -> str:
    """实时联网搜索工具。返回网页摘要。"""
    recent = _recent_search_queries.get()
    if recent.count(query) >= 1:
        return f"Error: 你已经搜索过 '{query}' 了，请直接基于已有结果作答。"
    
    recent.append(query)
    _recent_search_queries.set(recent)

    # ★ 新增：记录搜索耗时与状态
    start_time = time.perf_counter()
    try:
        _, fragments = await search_and_filter_pipeline(
            query=query, search_depth=search_depth, max_results=max_results,
        )
        duration = time.perf_counter() - start_time
        record_search(provider="tavily", status="success", duration=duration)
    except Exception as e:
        duration = time.perf_counter() - start_time
        record_search(provider="tavily", status="error", duration=duration)
        raise e

    if not fragments:
        result = json.dumps({"summary": "（未搜索到有效结果）", "sources": []}, ensure_ascii=False)
    else:
        summary_parts = []
        sources = []
        for i, (url, content, score) in enumerate(fragments, 1):
            summary_parts.append(f"<chunk id='{i}' source='{url}'>\n{content}\n</chunk>")
            sources.append({"url": url, "title": "", "snippet": content[:200]})

        summary = "<context_pool>\n" + "\n\n".join(summary_parts) + "\n</context_pool>"
        result = json.dumps({"summary": summary, "sources": sources}, ensure_ascii=False)

    status = _increment_and_get_status("search")
    try:
        payload = json.loads(result)
        payload["summary"] = payload.get("summary", "") + status
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return result + status


@tool
async def knowledge_search(query: str, top_k: int = 5, max_age_days: int = 0) -> str:
    """本地知识库检索工具。"""
    try:
        from rag import get_knowledge_base
    except ImportError:
        return json.dumps({"summary": "（知识库不可用）", "sources": []}, ensure_ascii=False)

    kb = get_knowledge_base()
    results = kb.search(query, top_k=min(max(top_k, 1), 10), max_age_days=max_age_days if max_age_days > 0 else None)
    status = _increment_and_get_status("knowledge_search")

    if not results:
        return json.dumps({"summary": "（知识库中未找到相关内容）" + status, "sources": []}, ensure_ascii=False)

    lines, sources = [], []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        source = meta.get("source", "未知来源")
        lines.append(f"[{i}] {source} (相关性: {r['score']:.2f})")
        lines.append(r["text"])
        lines.append("")
        sources.append({"url": meta.get("url", ""), "title": source, "snippet": r["text"][:200]})

    return json.dumps({"summary": "\n".join(lines) + status, "sources": sources}, ensure_ascii=False)


@tool
async def graph_search(query: str, top_k: int = 5, max_hops: int = 2) -> str:
    """知识图谱检索工具。"""
    try:
        from rag.structured import get_structured_index
    except ImportError:
        return json.dumps({"summary": "（知识图谱不可用）", "paths": [], "sources": []}, ensure_ascii=False)

    idx = get_structured_index()
    
    # ★ 新增：记录图谱检索耗时
    start_time = time.perf_counter()
    result = idx.search(query, top_k=min(max(top_k, 1), 10), max_hops=min(max(max_hops, 1), 3))
    duration = time.perf_counter() - start_time
    record_graph_search(duration)
    
    docs = result.get("docs", [])
    paths_text = idx.kg.format_paths(result.get("paths", [])) if hasattr(idx, "kg") else ""

    lines, sources = [], []
    for i, r in enumerate(docs, 1):
        meta = r.get("metadata", {})
        source = meta.get("source", "未知来源")
        lines.append(f"[{i}] {source} (相关性: {r['score']:.2f})")
        lines.append(r["text"])
        entities = r.get("entities", [])
        if entities:
            names = ", ".join(e.get("name", "") for e in entities)
            lines.append(f"    ↳ 关联实体: {names}")
        lines.append("")
        sources.append({"url": meta.get("url", ""), "title": source, "snippet": r["text"][:200]})

    if paths_text:
        lines.append("多跳推理路径:")
        lines.append(paths_text)
    if not docs and not paths_text:
        lines.append("（知识图谱中未找到相关内容）")

    status = _increment_and_get_status("graph_search")
    return json.dumps({"summary": "\n".join(lines) + status, "paths": paths_text, "sources": sources}, ensure_ascii=False)


@tool
def calculator(expression: str) -> str:
    """安全数学计算器。"""
    import ast
    import operator
    _ALLOWED_OPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
    }
    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        if isinstance(node, ast.BinOp):
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None: raise ValueError("不支持的运算符")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None: raise ValueError("不支持的一元运算符")
            return op(_eval(node.operand))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("表达式包含不允许的内容")

    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval(tree)
        if isinstance(result, float) and result.is_integer(): result = int(result)
        res_str = str(result)
    except Exception as exc:
        res_str = f"（计算失败：{exc}）"
    return res_str + _increment_and_get_status("calculator")


@tool
def current_time() -> str:
    """获取当前 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat() + _increment_and_get_status("current_time")


@tool
async def history_search(query: str, top_k: int = 5) -> str:
    """检索用户过往会话历史。"""
    user_id = get_current_user_id()
    if not user_id:
        return json.dumps({"query": query, "count": 0, "sessions": [], "note": "无法识别用户身份"}, ensure_ascii=False)
    try:
        from memory.user_store import get_user_store
    except ImportError:
        return json.dumps({"query": query, "count": 0, "sessions": [], "note": "历史存储不可用"}, ensure_ascii=False)
    store = get_user_store()
    hits = store.search_sessions(user_id=user_id, query=query, top_k=min(max(top_k, 1), 10))
    status = _increment_and_get_status("history_search")
    return json.dumps({"query": query, "count": len(hits), "sessions": hits, "status": status}, ensure_ascii=False)