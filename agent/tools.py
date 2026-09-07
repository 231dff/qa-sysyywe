"""
智能搜索助手 — ReAct 工具集

为 ReAct Agent 暴露的工具。每个工具的 docstring 会被 LangChain 转换为
JSON Schema，作为模型「按需加载、自主决策」的边界契约。

暴露的工具：
- search: 实时联网搜索（时效性信息）
- knowledge_search: 本地知识库检索（内部沉淀知识，RAG）

查询改写与相关性打分已并入模型推理，降级回答作为内部兜底、不暴露给模型。
各工具返回值是 JSON 字符串，包含 summary（给 LLM 阅读）+ sources（给前端展示）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.tools import tool

from tools.registry import search_and_filter_pipeline
from utils.helpers import format_search_context


@tool
async def search(
    query: str,
    search_depth: str = "advanced",
    max_results: int = 5,
) -> str:
    """实时联网搜索工具。当用户问题的答案需要最新、可能已过时、或超出你训练数据覆盖范围的信息时使用。

    【应调用】的典型场景：
    - 问题涉及时效性信息：最新新闻、股价、天气、赛事比分、事件时间线等
    - 用户明确要求「搜索」「查一下」「最新」「今天/今年/最近」等
    - 需要多角度佐证、或你不确定时（可多次调用，每次换一个更精确的查询词）

    【不应调用】的典型场景：
    - 常识性、知识性、可稳定回答的问题（直接作答即可）
    - 纯翻译、改写、计算、逻辑推理、代码生成类请求

    参数说明：
    - query: 搜索引擎友好的精确查询词，去除口语与礼貌用语，中文关键词优先
    - search_depth: "basic" 较快；"advanced" 更全面（默认）
    - max_results: 返回条数，2-10 之间，默认 5

    返回：JSON 字符串，其中 summary 是去重并按相关性降序排列的搜索结果摘要
    （含 URL 与内容片段）；sources 是结构化的来源列表。
    如果搜索失败或无结果，summary 为「（未搜索到有效结果）」，此时应据实告知用户而非编造。
    """
    # 复用现有「搜索 → 去重 → 裁剪」流水线（默认用 Tavily 原始分数，省 1 次 LLM 调用）
    _, fragments = await search_and_filter_pipeline(
        query=query,
        use_llm_scoring=False,
        search_depth=search_depth,
        max_results=max_results,
    )

    if not fragments:
        return json.dumps(
            {"summary": "（未搜索到有效结果）", "sources": []},
            ensure_ascii=False,
        )

    # fragments 形如 [(url, content, score), ...]，仅保留 top max_results 条
    trimmed = fragments[:max_results]
    summary = format_search_context(trimmed)
    sources = [
        {"url": url, "title": "", "snippet": content[:200]}
        for url, content, _score in trimmed
    ]

    return json.dumps({"summary": summary, "sources": sources}, ensure_ascii=False)


@tool
async def knowledge_search(
    query: str,
    top_k: int = 5,
    max_age_days: int = 0,
) -> str:
    """本地知识库检索工具。用于查询项目/团队内部沉淀的文档、制度、历史决策等知识。

    【应调用】的场景：
    - 问题指向内部文档、公司/项目规则、历史背景、既定流程等（而非互联网最新信息）
    - 用户明确要求查询「知识库」「内部资料」「我们的文档」等
    - 需要引用可追溯的内部来源时

    【不应调用】的场景：
    - 需要最新互联网实时信息（改用 search 工具）
    - 常识性问题（直接作答）

    参数说明：
    - query: 检索查询词，尽量用文档中可能出现的关键词
    - top_k: 返回条数，1-10，默认 5
    - max_age_days: 时效过滤，仅返回 N 天内的文档（0 = 不过滤）

    返回：JSON 字符串，summary 为按相关性与新鲜度排序的知识片段，
    sources 为结构化来源（含元数据）。
    """
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

    if not results:
        return json.dumps({"summary": "（知识库中未找到相关内容）", "sources": []}, ensure_ascii=False)

    lines = []
    sources = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        source = meta.get("source", "未知来源")
        ts = meta.get("timestamp", "")
        lines.append(f"[{i}] {source} (相关性: {r['score']:.2f})")
        lines.append(r["text"])
        lines.append("")
        sources.append({
            "url": meta.get("url", ""),
            "title": source,
            "snippet": r["text"][:200],
        })

    return json.dumps(
        {"summary": "\n".join(lines), "sources": sources},
        ensure_ascii=False,
    )


@tool
async def graph_search(
    query: str,
    top_k: int = 5,
    max_hops: int = 2,
) -> str:
    """知识图谱检索工具。用于需要**多跳推理**或**精确消歧**的垂直场景，
    例如医疗问诊（症状→疾病→用药）、法律案件（人物→关系→法条）、家族/组织关系（某人→关系→某人）等。

    【应调用】的场景：
    - 问题需要从「A 与 B 的关系」这类多步推导，而非单段文本能直接回答
    - 需要区分同名同型/不同型的实体（消歧）
    - 问题指向已沉淀到内部知识库的结构化关系（非互联网最新信息）

    【不应调用】的场景：
    - 实时互联网信息（改用 search 工具）
    - 普通内部文档片段检索（改用 knowledge_search 工具）

    参数说明：
    - query: 检索查询词，用关键实体名或问题关键词
    - top_k: 返回相关文档条数，1-10，默认 5
    - max_hops: 图谱多跳遍历深度，1-3，默认 2

    返回：JSON 字符串，summary 为相关文档片段（含关联实体），
    paths 为多跳推理路径；sources 为结构化来源。
    """
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

    return json.dumps(
        {"summary": "\n".join(lines), "paths": paths_text, "sources": sources},
        ensure_ascii=False,
    )


@tool
def calculator(expression: str) -> str:
    """安全数学计算器。用于需要精确数值计算的场景（算术、百分比、单位换算等）。

    【应调用】的场景：
    - 用户要求计算、换算、求百分比、加减乘除等精确数值
    - 回答中需要给出准确计算结果

    【不应调用】的场景：
    - 涉及代码执行、文件操作、网络访问等（会被拒绝）

    参数说明：
    - expression: 算术表达式，支持 + - * / // % ** 与括号，如 "3.14 * 2 ** 2"

    返回：计算结果字符串，或错误说明。
    """
    import ast
    import operator

    # 白名单 AST 求值，杜绝任意代码执行
    _ALLOWED_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
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
        return str(result)
    except Exception as exc:
        return f"（计算失败：{exc}）"


@tool
def current_time() -> str:
    """获取当前 UTC 时间。用于回答涉及「现在几点」「今天日期」「距离某日期多久」等需要精确时间的场景。

    返回：ISO 8601 格式的当前 UTC 时间字符串。
    """
    return datetime.now(timezone.utc).isoformat()

