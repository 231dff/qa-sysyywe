"""
智能搜索助手 — ReAct 工具集 (增加主动网页深读与防死循环)
"""
from __future__ import annotations

import os
os.environ["LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"] = "0"

import json
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from langgraph.errors import GraphRecursionError

from tools.registry import search_and_filter_pipeline
from tools.web_extractor import fetch_and_extract
from utils.http_client import get_http_client
from utils.llm_context import (
    get_llm_context,
    set_llm_context,
)
from utils.llm_factory import build_llm
from config import get_api_config

# ============================================================
# 全局上下文变量
# ============================================================
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
    warning = ""
    if total_calls >= 6:
        warning = " ⚠️ 注意：你已调用工具多次，请立刻收敛并直接输出最终答案，严禁继续搜索！"

    return f"\n\n<agent_status>[系统状态栏] 当前工具调用统计 -> {stats_str}。{warning}</agent_status>"


# ============================================================
# 子 Agent 上下文隔离 (带防死循环熔断)
# ============================================================
WORKER_PROMPT = """你是一个专业的资料检索子 Agent。
你的任务是根据指令去联网搜索信息，并提炼出最核心的事实与数据。

## 纪律要求（极其重要）：
1. 你最多只能调用 2 次搜索工具！如果 2 次搜索后信息仍不足，请直接基于已有信息作答。
2. 严禁使用完全相同的搜索词重复搜索。
3. 请直接输出结构化的要点，不要输出废话，不要包含你正在使用工具的描述。"""


@tool
async def spawn_search_worker(query: str) -> str:
    """委派一个子 Agent 去独立完成复杂的检索任务。
    当需要查询多个独立实体（如对比多款产品），或者需要阅读大量长文时，请调用此工具。
    子 Agent 会在自己的隔离环境中完成搜索，并只把提炼好的摘要返回给你。
    """
    current_depth = _agent_depth.get()
    if current_depth >= 1:
        return "Error: 已经处于子任务中，严禁再次委派，请直接使用当前上下文作答。"

    llm_ctx = get_llm_context()
    current_key = llm_ctx["api_key"]
    current_base = llm_ctx["api_base"]
    current_model = llm_ctx["model"]

    if not current_key:
        api_cfg = get_api_config()
        current_key = api_cfg.llm_api_key.get_secret_value()
        current_base = api_cfg.llm_api_base
        current_model = "qwen-turbo"

    # ★ 核心修复：去掉了 provider=None
    worker_llm = build_llm(
        model=current_model,
        base_url=current_base,
        api_key=current_key,
        temperature=0.0,
        streaming=False,
    )

    @tool
    async def local_search(q: str) -> str:
        """底层联网深度搜索"""
        recent = _recent_search_queries.get()
        if recent.count(q) >= 1:
            return f"Error: 你刚刚已经搜索过 '{q}' 了，请更换搜索词，或者直接基于已有结果作答。"
        
        recent.append(q)
        _recent_search_queries.set(recent)

        _, fragments = await search_and_filter_pipeline(query=q, max_results=5)
        if not fragments:
            return "（未搜索到有效结果）"

        parts = []
        for i, (url, content, score) in enumerate(fragments, 1):
            parts.append(f"[来源 {i} ({url})] \n{content}")
        return "\n\n".join(parts)

    _agent_depth.set(current_depth + 1)
    _recent_search_queries.set([])

    sub_agent = create_agent(
        model=worker_llm,
        tools=[local_search],
        system_prompt=WORKER_PROMPT,
    )

    try:
        result = await sub_agent.ainvoke(
            {"messages": [HumanMessage(content=f"请帮我详细检索并总结以下信息：{query}")]},
            config={"recursion_limit": 10}
        )
        final_text = result["messages"][-1].content
    except GraphRecursionError:
        final_text = "（子任务检索步骤过多，已强制中断。请基于目前查到的事实直接回答用户。）"
    except Exception as e:
        final_text = f"（子任务检索异常: {str(e)}）"
    finally:
        _agent_depth.set(current_depth)
        _recent_search_queries.set([])

    status = _increment_and_get_status("spawn_search_worker")
    return f"{final_text}\n{status}"


# ============================================================
# 主 Agent 工具
# ============================================================
@tool
async def read_tool_output(file_path: str) -> str:
    """读取因为过长而被截断落盘的完整内容。"""
    if not file_path.startswith("/tmp/tool_outputs/"):
        return "Error: 安全限制，只能读取工具输出目录下的文件。"
    if not os.path.exists(file_path):
        return f"Error: 文件 {file_path} 不存在或已被清理。"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            status = _increment_and_get_status("read_tool_output")
            return content + status
    except Exception as e:
        return f"Error: 读取文件失败: {str(e)}"


@tool
async def fetch_webpage_content(url: str) -> str:
    """当你发现搜索返回的摘要被折叠（比如显示"...中间部分已折叠..."），
    或者你认为需要阅读网页的完整全貌才能回答用户时，调用此工具深度阅读指定 URL 的网页正文。"""
    try:
        async with get_http_client(timeout=15.0) as client:
            content = await fetch_and_extract(url, client)
            if not content:
                return "（该网页内容无法提取或为空）"
            
            max_chars = 10000
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n...（内容过长，已截断）..."
                
            status = _increment_and_get_status("fetch_webpage_content")
            return f"<webpage_content url='{url}'>\n{content}\n</webpage_content>{status}"
    except Exception as e:
        return f"读取网页失败: {str(e)}"


@tool
async def search(
    query: str,
    search_depth: str = "advanced",
    max_results: int = 5,
) -> str:
    """实时联网深度搜索工具。内部已完成网页精读与重排，直接返回高密度上下文池。"""
    recent = _recent_search_queries.get()
    if recent.count(query) >= 1:
        return f"Error: 你已经搜索过 '{query}' 了，请更换思路或搜索词，不要重复调用。"
    
    recent.append(query)
    _recent_search_queries.set(recent)

    _, fragments = await search_and_filter_pipeline(
        query=query,
        search_depth=search_depth,
        max_results=max_results,
    )

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
    result = idx.search(query, top_k=min(max(top_k, 1), 10), max_hops=min(max(max_hops, 1), 3))
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
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BinOp):
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None:
                raise ValueError("不支持的运算符")
            return op(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _ALLOWED_OPS.get(type(node.op))
            if op is None:
                raise ValueError("不支持的一元运算符")
            return op(_eval(node.operand))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("表达式包含不允许的内容")

    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval(tree)
        if isinstance(result, float) and result.is_integer():
            result = int(result)
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