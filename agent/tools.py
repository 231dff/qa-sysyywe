"""
智能搜索助手 — ReAct 工具集
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool

from tools.registry import search_and_filter_pipeline
from utils.helpers import format_search_context

# ============================================================
# 全局上下文变量（用于 Agent 状态栏）
# ============================================================
_current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")

# 记录当前请求的工具调用次数
_tool_call_counts: ContextVar[dict] = ContextVar("tool_call_counts", default={})

def set_current_user_id(user_id: str) -> None:
    _current_user_id.set(user_id or "")

def get_current_user_id() -> str:
    return _current_user_id.get()

def reset_tool_counts() -> None:
    """每次新的对话开始时重置计数"""
    _tool_call_counts.set({})

def _increment_and_get_status(tool_name: str) -> str:
    """增加调用次数，并生成状态栏字符串"""
    counts = _tool_call_counts.get()
    counts[tool_name] = counts.get(tool_name, 0) + 1
    _tool_call_counts.set(counts)
    
    # 生成类似: "search: 2 次, read_tool_output: 1 次"
    stats_str = ", ".join([f"{k}: {v} 次" for k, v in counts.items()])
    
    # 加上预算警告
    total_calls = sum(counts.values())
    warning = ""
    if total_calls >= 10:
        warning = " ⚠️ 注意：你已调用工具较多，请尽快收敛并作答，不要无限探索。"

    return f"\n\n<agent_status>[系统状态栏] 当前工具调用统计 -> {stats_str}。请合理规划剩余步数。{warning}</agent_status>"


# ============================================================
# 新增工具：读取长输出落盘文件
# ============================================================
@tool
async def read_tool_output(file_path: str) -> str:
    """读取因为过长而被截断落盘的完整内容。
    当 search 工具返回的 summary 末尾提示“原始搜索结果较长...请调用 read_tool_output”，
    且你发现当前摘要信息不足以回答用户问题时，调用此工具读取完整文件。
    
    Args:
        file_path: 文件路径（如 /tmp/tool_outputs/search_a1b2c3d4.json）
    """
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


# ============================================================
# 原有工具：实时搜索
# ============================================================
@tool
async def search(
    query: str,
    search_depth: str = "advanced",
    max_results: int = 5,
) -> str:
    """实时联网搜索工具。当用户问题的答案需要最新、可能已过时、或超出你训练数据覆盖范围的信息时使用。
    (说明：为了简洁，此处省略长篇 docstring，请保留你原有的详细 docstring)
    """
    _, fragments = await search_and_filter_pipeline(
        query=query,
        use_llm_scoring=False,
        search_depth=search_depth,
        max_results=max_results,
    )

    if not fragments:
        result = json.dumps(
            {"summary": "（未搜索到有效结果）", "sources": []},
            ensure_ascii=False,
        )
    else:
        real_fragments = [f for f in fragments if f[0] != "system_tip"]
        tips = [f[1] for f in fragments if f[0] == "system_tip"]

        trimmed = real_fragments[:max_results]
        summary = format_search_context(trimmed)
        
        if tips:
            summary += "\n\n" + "\n".join(tips)

        sources = [
            {"url": url, "title": "", "snippet": content[:200]}
            for url, content, _score in trimmed
        ]
        result = json.dumps({"summary": summary, "sources": sources}, ensure_ascii=False)

    # ★ 注入状态栏
    status = _increment_and_get_status("search")
    # 如果是 JSON 字符串，我们需要把状态拼接到 summary 里
    try:
        payload = json.loads(result)
        payload["summary"] = payload.get("summary", "") + status
        return json.dumps(payload, ensure_ascii=False)
    except:
        return result + status


# ============================================================
# 原有工具：知识库检索 (同样注入状态栏)
# ============================================================
@tool
async def knowledge_search(
    query: str,
    top_k: int = 5,
    max_age_days: int = 0,
) -> str:
    """本地知识库检索工具。用于查询项目/团队内部沉淀的文档、制度、历史决策等知识。"""
    try:
        from rag import get_knowledge_base
    except ImportError:
        return json.dumps({"summary": "（知识库不可用）", "sources": []}, ensure_ascii=False)

    kb = get_knowledge_base()
    results = kb.search(
        query,
        top_k=min(max(top_k, 1), 10),
        max_age_days=max_age_days if max_age_days > 0 else None,
    )

    status = _increment_and_get_status("knowledge_search")

    if not results:
        return json.dumps({"summary": "（知识库中未找到相关内容）" + status, "sources": []}, ensure_ascii=False)

    lines = []
    sources = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        source = meta.get("source", "未知来源")
        lines.append(f"[{i}] {source} (相关性: {r['score']:.2f})")
        lines.append(r["text"])
        lines.append("")
        sources.append({
            "url": meta.get("url", ""),
            "title": source,
            "snippet": r["text"][:200],
        })

    return json.dumps(
        {"summary": "\n".join(lines) + status, "sources": sources},
        ensure_ascii=False,
    )


# ============================================================
# 原有工具：知识图谱 (注入状态栏)
# ============================================================
@tool
async def graph_search(
    query: str,
    top_k: int = 5,
    max_hops: int = 2,
) -> str:
    """知识图谱检索工具。用于需要多跳推理/精确消歧的垂直场景。"""
    try:
        from rag.structured import get_structured_index
    except ImportError:
        return json.dumps({"summary": "（知识图谱不可用）", "paths": [], "sources": []}, ensure_ascii=False)

    idx = get_structured_index()
    result = idx.search(
        query,
        top_k=min(max(top_k, 1), 10),
        max_hops=min(max(max_hops, 1), 3),
    )

    docs = result.get("docs", [])
    paths_text = idx.kg.format_paths(result.get("paths", [])) if hasattr(idx, "kg") else ""

    lines = []
    sources = []
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
        sources.append({
            "url": meta.get("url", ""),
            "title": source,
            "snippet": r["text"][:200],
        })

    if paths_text:
        lines.append("多跳推理路径:")
        lines.append(paths_text)

    if not docs and not paths_text:
        lines.append("（知识图谱中未找到相关内容）")

    status = _increment_and_get_status("graph_search")

    return json.dumps(
        {"summary": "\n".join(lines) + status, "paths": paths_text, "sources": sources},
        ensure_ascii=False,
    )


# ============================================================
# 原有工具：计算器 (注入状态栏)
# ============================================================
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
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        res_str = str(result)
    except Exception as exc:
        res_str = f"（计算失败：{exc}）"

    status = _increment_and_get_status("calculator")
    return res_str + status


# ============================================================
# 原有工具：时间获取
# ============================================================
@tool
def current_time() -> str:
    """获取当前 UTC 时间。"""
    status = _increment_and_get_status("current_time")
    return datetime.now(timezone.utc).isoformat() + status


# ============================================================
# 原有工具：历史检索
# ============================================================
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
    
    return json.dumps({
        "query": query, 
        "count": len(hits), 
        "sessions": hits,
        "status": status
    }, ensure_ascii=False)