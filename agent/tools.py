"""
智能搜索助手 — ReAct 工具集
"""
from __future__ import annotations

import os
# ★ 双保险：强制在模块加载时禁用 LangChain 底层流式看门狗
os.environ["LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"] = "0"

import json
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from tools.registry import search_and_filter_pipeline
from utils.helpers import format_search_context
from config import get_api_config, resolve_llm_connection, get_agent_config

# ============================================================
# 全局上下文变量（用于 Agent 状态栏与动态配置）
# ============================================================
_current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")
_tool_call_counts: ContextVar[dict] = ContextVar("tool_call_counts", default={})

# ★ 用于透传给子 Agent 的 LLM 配置
_llm_api_key: ContextVar[str] = ContextVar("llm_api_key", default="")
_llm_api_base: ContextVar[str] = ContextVar("llm_api_base", default="")
_llm_model: ContextVar[str] = ContextVar("llm_model", default="")

def set_current_user_id(user_id: str) -> None:
    _current_user_id.set(user_id or "")

def get_current_user_id() -> str:
    return _current_user_id.get()

def reset_tool_counts() -> None:
    _tool_call_counts.set({})

def set_llm_context(api_key: str, api_base: str, model: str):
    """由主 Agent 注入当前生效的 LLM 配置"""
    _llm_api_key.set(api_key)
    _llm_api_base.set(api_base)
    _llm_model.set(model)

def _increment_and_get_status(tool_name: str) -> str:
    counts = _tool_call_counts.get()
    counts[tool_name] = counts.get(tool_name, 0) + 1
    _tool_call_counts.set(counts)
    
    stats_str = ", ".join([f"{k}: {v} 次" for k, v in counts.items()])
    total_calls = sum(counts.values())
    warning = ""
    if total_calls >= 10:
        warning = " ⚠️ 注意：你已调用工具较多，请尽快收敛并作答，不要无限探索。"

    return f"\n\n<agent_status>[系统状态栏] 当前工具调用统计 -> {stats_str}。请合理规划剩余步数。{warning}</agent_status>"


# ============================================================
# 子 Agent 上下文隔离 (Manager-Worker 模式)
# ============================================================
WORKER_PROMPT = """你是一个专业的资料检索子 Agent。
你的任务是根据指令去联网搜索信息，并提炼出最核心的事实与数据。
系统已经为你自动完成了网页抓取和语义重排，你收到的结果是高度浓缩的上下文段落。

请直接输出结构化的要点（如 Markdown 列表），不要输出任何废话，不要包含你正在使用工具的描述。"""

@tool
async def spawn_search_worker(query: str) -> str:
    """委派一个子 Agent 去独立完成复杂的检索任务。
    当需要查询多个独立实体（如对比多款产品），或者需要阅读大量长文时，请调用此工具。
    子 Agent 会在自己的隔离环境中完成搜索，并只把提炼好的摘要返回给你。
    """
    current_key = _llm_api_key.get()
    current_base = _llm_api_base.get()
    current_model = _llm_model.get()
    
    if not current_key:
        api_cfg = get_api_config()
        current_key = api_cfg.llm_api_key.get_secret_value()
        current_base = api_cfg.llm_api_base
        current_model = "qwen-turbo"
    
    # ★ 核心修复：同样移除不被支持的参数
    worker_llm = ChatOpenAI(
        model=current_model, 
        base_url=current_base,
        api_key=current_key,
        temperature=0.0,
        max_retries=3,
    )
    
    @tool
    async def local_search(q: str) -> str:
        """底层联网深度搜索"""
        _, fragments = await search_and_filter_pipeline(query=q, max_results=5)
        if not fragments:
            return "（未搜索到有效结果）"
            
        # 拼接清洗后的文本片段
        parts = []
        for i, (url, content, score) in enumerate(fragments, 1):
            parts.append(f"[来源 {i} ({url})] \n{content}")
        return "\n\n".join(parts)

    sub_agent = create_react_agent(
        worker_llm,
        tools=[local_search],
        prompt=WORKER_PROMPT
    )
    
    result = await sub_agent.ainvoke(
        {"messages": [HumanMessage(content=f"请帮我详细检索并总结以下信息：{query}")]},
        config={"recursion_limit": 10}
    )
    
    final_text = result["messages"][-1].content
    status = _increment_and_get_status("spawn_search_worker")
    
    return f"{final_text}\n{status}"


# ============================================================
# 主 Agent 工具
# ============================================================
@tool
async def read_tool_output(file_path: str) -> str:
    """读取因为过长而被截断落盘的完整内容。（注：升级后搜索工具已极少触发此机制）"""
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
async def search(
    query: str,
    search_depth: str = "advanced",
    max_results: int = 5,
) -> str:
    """实时联网深度搜索工具。内部已完成网页精读与重排，直接返回高密度上下文池。"""
    _, fragments = await search_and_filter_pipeline(
        query=query,
        search_depth=search_depth,
        max_results=max_results,
    )

    if not fragments:
        result = json.dumps({"summary": "（未搜索到有效结果）", "sources": []}, ensure_ascii=False)
    else:
        # ★ 组装为高密度上下文池，直接喂给大模型
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
    except:
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
    import ast, operator
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