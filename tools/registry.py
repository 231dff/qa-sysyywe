"""
智能搜索助手 — 工具注册中心
按职责单一原则注册所有工具：
1. Tavily 搜索
2. LLM 查询改写
3. LLM 相关性打分 (保留作为备用)
4. LLM 降级回答
5. 深度检索流水线 (多路召回 + 流式网页精读 + Rerank)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

import httpx

from agent.models import (
    RelevanceScore,
    RelevanceScores,
    RewrittenQuery,
    SearchResponse,
    SearchResult,
    ToolCategory,
    ToolMetadata,
)
from config import get_agent_config, get_api_config
from utils.helpers import (
    URLDeduplicator,
    count_tokens,
    exponential_backoff,
    format_search_context,
    trim_context,
)
from utils.http_client import get_http_client
from utils.metrics import (
    record_error,
    record_llm_call,
    record_search,
    record_tokens,
)

# ★ 引入新增的流式网页提取和重排模块
from tools.web_extractor import stream_extract_contents, chunk_text
from tools.reranker import rerank_chunks

# ============================================================
# 工具元数据注册表
# ============================================================
TOOL_REGISTRY: dict[str, ToolMetadata] = {
    "tavily_search": ToolMetadata(
        name="tavily_search",
        category=ToolCategory.SEARCH,
        description="使用 Tavily API 执行实时联网搜索，返回 URL、标题、摘要及相关性分数",
    ),
    "query_rewriter": ToolMetadata(
        name="query_rewriter",
        category=ToolCategory.REWRITE,
        description="LLM 驱动的查询改写：将口语化提问转换为精确的搜索关键词",
    ),
    "relevance_scorer": ToolMetadata(
        name="relevance_scorer",
        category=ToolCategory.RELEVANCE,
        description="LLM 驱动的相关性打分：对搜索结果进行 0-1 评分并给出理由",
    ),
    "fallback_answer": ToolMetadata(
        name="fallback_answer",
        category=ToolCategory.FALLBACK,
        description="LLM 降级回答：当搜索不可用时，基于 LLM 参数化知识生成答案",
    ),
}

# ============================================================
# Tool 1: Tavily 搜索
# ============================================================
@exponential_backoff()
async def tavily_search(
    query: str,
    max_results: Optional[int] = None,
    search_depth: Optional[str] = None,
    include_domains: Optional[list[str]] = None,
    exclude_domains: Optional[list[str]] = None,
) -> SearchResponse:
    """
    调用 Tavily Search API 执行实时搜索
    使用 httpx 异步客户端，支持超时与重试
    """
    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    max_results = max_results or agent_cfg.max_search_results
    search_depth = search_depth or agent_cfg.search_depth
    include_domains = include_domains or agent_cfg.include_domains
    exclude_domains = exclude_domains or agent_cfg.exclude_domains

    payload = {
        "api_key": api_cfg.tavily_api_key.get_secret_value(),
        "query": query,
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    start = time.perf_counter()

    try:
        async with get_http_client(timeout=agent_cfg.search_timeout) as client:
            resp = await client.post(
                f"{api_cfg.tavily_api_base}/search",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append(SearchResult(
                url=item.get("url", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                score=item.get("score", 0.0),
                raw_response=item,
            ))

        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="success", duration=elapsed_ms / 1000)

        return SearchResponse(
            query=query,
            results=results,
            total_count=len(results),
            search_time_ms=elapsed_ms,
        )

    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="timeout", duration=elapsed_ms / 1000)
        return SearchResponse(
            query=query,
            results=[],
            search_time_ms=elapsed_ms,
            error="搜索 API 超时",
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="error", duration=elapsed_ms / 1000)
        return SearchResponse(
            query=query,
            results=[],
            search_time_ms=elapsed_ms,
            error=f"搜索失败: {str(exc)}",
        )

# ============================================================
# Tool 2: LLM 查询改写
# ============================================================
REWRITE_SYSTEM_PROMPT = """你是一个搜索查询优化专家。你的任务是将用户的自然语言提问改写为精准的搜索引擎查询词。

## 规则
1. 提取核心关键词，去掉礼貌用语和冗余修饰
2. 如为最新/实时问题，加上 "2024" 或 "2025" 等时间限定词
3. 对于复合问题，拆分为 1-3 个子查询
4. 检测用户语言，输出对应语言的查询词
5. 识别意图类型: factual(事实查询), news(新闻), opinion(观点), guide(教程), comparison(对比)

## 输出格式 (严格遵守 JSON)
{
  "rewritten": "改写后的主查询词",
  "language": "zh",
  "intent": "news",
  "sub_queries": ["子查询1", "子查询2"]
}"""

async def rewrite_query(
    user_query: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> RewrittenQuery:
    """
    使用 LLM 将用户口语化提问改写为精确搜索查询
    结合对话历史理解上下文意图
    """
    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    history_text = ""
    if conversation_history:
        recent = conversation_history[-6:]
        turns = []
        for msg in recent:
            role = "用户" if msg["role"] == "user" else "助手"
            turns.append(f"{role}: {msg['content'][:200]}")
        history_text = "\n".join(turns)

    user_message = f"""## 对话历史
{history_text or "(无)"}

## 当前用户问题
{user_query}

请输出 JSON:"""

    start = time.perf_counter()
    try:
        request_body = {
            "model": api_cfg.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        if api_cfg.supports_response_format:
            request_body["response_format"] = {"type": "json_object"}

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

        elapsed = time.perf_counter() - start
        record_llm_call("rewrite", elapsed)
        usage = data.get("usage", {})
        record_tokens(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        return RewrittenQuery(
            original=user_query,
            rewritten=parsed.get("rewritten", user_query),
            language=parsed.get("language", "zh"),
            intent=parsed.get("intent", "factual"),
            sub_queries=parsed.get("sub_queries", []),
        )

    except Exception as exc:
        elapsed = time.perf_counter() - start
        record_llm_call("rewrite", elapsed)
        record_error("llm_error")
        if agent_cfg.verbose:
            print(f"[查询改写失败] {exc}, 回退到原始查询")
        return RewrittenQuery(
            original=user_query,
            rewritten=user_query,
        )

# ============================================================
# Tool 3: LLM 相关性打分 (备用)
# ============================================================
RELEVANCE_SYSTEM_PROMPT = """你是一个搜索结果质量评估专家。对每条搜索结果与用户问题的相关性打分 (0-1):

## 评分维度
1. **直接性** (权重最高): 是否直接回答了用户的具体问题，而非仅涉及同一话题
2. **时效性**: 内容的时间是否符合问题的时效需求
3. **信息密度**: 单位文本内包含的有效信息量

## 评分标准
- 1.0: 直接、完整回答了用户问题
- 0.7-0.9: 包含核心答案，可能需要与其他来源补充
- 0.4-0.6: 提供有用背景，但不直接回答问题
- 0.1-0.3: 仅涉及相同话题，无法从中提取答案
- 0.0: 完全不相关

## 关键规则
- "聊到同一话题" ≠ "回答了问题"，前者最高只能 0.3
- 涉及"最新/近期/今年"的问题，过期内容的分数必须降 0.2+

## 输出格式 (严格遵守 JSON)
{
  "results": [
    {"url": "原样返回URL", "title": "原样返回标题", "relevance": 0.85, "reason": "简短中文理由(≤20字)"}
  ]
}"""

async def score_relevance(
    user_query: str,
    search_results: list[SearchResult],
) -> RelevanceScores:
    """使用 LLM 对搜索结果逐一打分，过滤低质量结果"""
    if not search_results:
        return RelevanceScores(results=[])

    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    candidates = []
    for i, r in enumerate(search_results):
        candidates.append({
            "index": i,
            "url": r.url,
            "title": r.title,
            "content": r.content[:500],
        })

    user_message = f"""## 用户问题
{user_query}

## 搜索结果候选
{json.dumps(candidates, ensure_ascii=False, indent=2)}

请为每条结果打分:"""

    try:
        request_body = {
            "model": api_cfg.llm_model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        if api_cfg.supports_response_format:
            request_body["response_format"] = {"type": "json_object"}

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

        scores = []
        for item in parsed.get("results", []):
            scores.append(RelevanceScore(
                url=item.get("url", ""),
                title=item.get("title", ""),
                relevance=float(item.get("relevance", 0.0)),
                reason=item.get("reason", ""),
            ))

        return RelevanceScores(results=scores)

    except Exception as exc:
        if agent_cfg.verbose:
            print(f"[相关性打分失败] {exc}, 回退到原始分数")
        fallback = []
        for r in search_results:
            fallback.append(RelevanceScore(
                url=r.url,
                title=r.title,
                relevance=r.score if r.score else 0.5,
                reason="原始搜索分数",
            ))
        return RelevanceScores(results=fallback)

# ============================================================
# Tool 4: LLM 降级回答
# ============================================================
FALLBACK_SYSTEM_PROMPT = """你是一个智能问答助手。由于实时搜索暂时不可用，请基于你的参数化知识回答用户问题。

## 重要规则
1. 在回答开头明确说明: "⚠️ 当前无法进行实时搜索，以下回答基于我的训练数据"
2. 如果知道答案，直接回答；不知道就诚实说明
3. 如果问题涉及最新信息你无法确认，诚实说明知识截止日期限制
4. 给出一般性建议或已知背景信息
5. 结尾建议用户稍后重试或提供更具体的问题

## 输出格式要求
- **开头**: ⚠️ 声明 + 一句话直接回答
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条 1-2 句话
- **禁止**: 不要用大段文字、不要多级标题
- **格式**: 使用 Markdown，保持简洁——多用列表，少用段落"""

async def generate_fallback_answer(
    user_query: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> str:
    """当搜索 API 不可用时，降级为纯 LLM 知识回答"""
    api_cfg = get_api_config()

    messages = [{"role": "system", "content": FALLBACK_SYSTEM_PROMPT}]
    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_query})

    start = time.perf_counter()
    try:
        async with get_http_client(timeout=60.0) as client:
            resp = await client.post(
                f"{api_cfg.llm_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_cfg.llm_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": api_cfg.llm_model,
                    "temperature": 0.5,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"]

        elapsed = time.perf_counter() - start
        record_llm_call("fallback", elapsed)
        record_error("fallback_triggered")
        usage = data.get("usage", {})
        record_tokens(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )
        return answer

    except Exception as exc:
        elapsed = time.perf_counter() - start
        record_llm_call("fallback", elapsed)
        record_error("llm_error")
        return f"抱歉，当前无法生成回答。请稍后重试。（错误: {str(exc)}）"

# ============================================================
# Tool 5: 组合工具 - 生产级 DeepSeek 搜索机制
# ============================================================
async def search_and_filter_pipeline(
    query: str,
    deduplicator: Optional[URLDeduplicator] = None,
    use_llm_scoring: bool = False, # 保留参数，但现在主要靠 Rerank
    search_depth: Optional[str] = None,
    max_results: Optional[int] = None,
) -> tuple[list[SearchResult], list[tuple[str, str, float]]]:
    """
    生产级 DeepSeek 搜索机制：
    1. Query 拆解并发多路召回
    2. ★ 流式网页精读抓取 (带时间预算)
    3. 细粒度段落切分 (Chunking)
    4. Rerank 语义重排
    5. 组装精炼上下文池
    """
    agent_cfg = get_agent_config()
    if deduplicator is None:
        deduplicator = URLDeduplicator(window_size=agent_cfg.dedup_window)

    # ---- Step 1: 多查询拆解与并发搜索 ----
    rewritten = await rewrite_query(query)
    search_queries = [rewritten.rewritten] + (rewritten.sub_queries or [])
    search_queries = list(dict.fromkeys(search_queries))[:3] 
    
    all_results = []
    search_tasks = [tavily_search(q, max_results=5, search_depth=search_depth) for q in search_queries]
    responses = await asyncio.gather(*search_tasks)
    
    for resp in responses:
        if not resp.error:
            all_results.extend(resp.results)

    # ---- Step 2: URL 去重 ----
    deduped: list[SearchResult] = []
    for r in all_results:
        if not deduplicator.is_duplicate(r.url):
            deduped.append(r)
            deduplicator.add(r.url)

    if not deduped:
        return [], []

    # ---- Step 3: ★ 流式网页精读抓取 (带时间预算) ----
    urls_to_fetch = [r.url for r in deduped[:6]]
    all_chunks = []
    
    if agent_cfg.verbose: 
        print(f"[Pipeline] 开始流式抓取 {len(urls_to_fetch)} 个网页，时间预算 6.0s...")
    
    # 通过异步生成器边抓取边处理
    async for _, content in stream_extract_contents(urls_to_fetch, time_budget=6.0):
        # 拿到一个网页正文，立刻切片进入候选池
        chunks = chunk_text(content, chunk_size=600, overlap=100)
        for c in chunks:
            # 这里可以用 source_url 标识，但由于生成器简化了返回值，直接用 placeholder
            all_chunks.append({"url": "web_source", "title": "", "text": c})

    # 兜底：如果所有网页都抓取失败（比如全部超时），降级使用 Snippet
    if not all_chunks:
        if agent_cfg.verbose: print("[Pipeline] 网页抓取全部失败，降级使用搜索引擎 Snippet")
        for r in deduped:
            all_chunks.append({"url": r.url, "title": r.title, "text": r.content, "score": r.score or 0.5})
        top_chunks = all_chunks[:agent_cfg.top_k_fragments]
    else:
        # ---- Step 4: Rerank 重排序 ----
        if agent_cfg.verbose: print(f"[Pipeline] 收集到 {len(all_chunks)} 个段落，开始 Rerank 重排...")
        # 使用本地轻量级 TF-IDF Rerank
        top_chunks = rerank_chunks(query, all_chunks, top_k=agent_cfg.top_k_fragments)
    
    # ---- Step 5: 组装最终的上下文片段 ----
    fragments: list[tuple[str, str, float]] = []
    for c in top_chunks:
        # 组装格式：(URL, 文本, 分数)
        fragments.append((c["url"], c["text"], c.get("score", 0.5)))

    if agent_cfg.verbose:
        print(f"[Pipeline] 检索流程完成，注入 {len(fragments)} 个精炼段落。")

    return deduped, fragments