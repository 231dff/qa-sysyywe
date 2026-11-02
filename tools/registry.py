"""
智能搜索助手 — 工具注册中心 (DeepSeek 级动态预算与防爆版)

★ 超时契约：共享客户端模式下，所有 client.post 必须显式传 timeout=
★ LLM 配置：从 utils.llm_context 读取请求级配置，回退到 .env
★ 动态预算：全局限制上下文 6000 tokens，单篇限制 2500 tokens，防撑爆大模型
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
    RelevanceScore, RelevanceScores, RewrittenQuery,
    SearchResponse, SearchResult, ToolCategory, ToolMetadata,
)
from config import get_agent_config, get_api_config
from utils.helpers import (
    URLDeduplicator, count_tokens, exponential_backoff,
    format_search_context, trim_context,
)
from utils.http_client import get_http_client
from utils.llm_context import get_llm_context
from utils.metrics import (
    record_error, record_llm_call, record_search, record_tokens,
)
from tools.web_extractor import (
    stream_extract_contents, chunk_text, smart_truncate_by_token, count_tokens as count_tokens_extractor
)
from tools.reranker import rerank_chunks


TOOL_REGISTRY: dict[str, ToolMetadata] = {
    "tavily_search": ToolMetadata(
        name="tavily_search", category=ToolCategory.SEARCH,
        description="使用 Tavily API 执行实时联网搜索",
    ),
    "query_rewriter": ToolMetadata(
        name="query_rewriter", category=ToolCategory.REWRITE,
        description="LLM 驱动的查询改写",
    ),
    "relevance_scorer": ToolMetadata(
        name="relevance_scorer", category=ToolCategory.RELEVANCE,
        description="LLM 驱动的相关性打分",
    ),
    "fallback_answer": ToolMetadata(
        name="fallback_answer", category=ToolCategory.FALLBACK,
        description="LLM 降级回答",
    ),
}


def _resolve_llm() -> dict:
    cfg = get_api_config()
    ctx = get_llm_context()
    model = ctx["model"] or cfg.llm_model
    base = ctx["api_base"] or cfg.llm_api_base
    key = ctx["api_key"] or cfg.llm_api_key.get_secret_value()

    supports_response_format = True
    model_lower = (model or "").lower()
    if "deepseek" in model_lower or "qwq" in model_lower:
        supports_response_format = False

    return {
        "model": model, "base": base, "key": key,
        "supports_response_format": supports_response_format,
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
    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    max_results = max_results or agent_cfg.max_search_results
    search_depth = search_depth or agent_cfg.search_depth
    include_domains = include_domains or agent_cfg.include_domains
    exclude_domains = exclude_domains or agent_cfg.exclude_domains

    payload = {
        "api_key": api_cfg.tavily_api_key.get_secret_value(),
        "query": query, "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False, "include_raw_content": False, "include_images": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    start = time.perf_counter()
    timeout_s = agent_cfg.search_timeout

    try:
        async with get_http_client(timeout=timeout_s) as client:
            resp = await client.post(
                f"{api_cfg.tavily_api_base}/search",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()

        results = [
            SearchResult(
                url=item.get("url", ""), title=item.get("title", ""),
                content=item.get("content", ""), score=item.get("score", 0.0),
                raw_response=item,
            )
            for item in data.get("results", [])
        ]
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="success", duration=elapsed_ms / 1000)
        return SearchResponse(query=query, results=results, total_count=len(results), search_time_ms=elapsed_ms)
    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="timeout", duration=elapsed_ms / 1000)
        return SearchResponse(query=query, results=[], search_time_ms=elapsed_ms, error="搜索 API 超时")
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="error", duration=elapsed_ms / 1000)
        return SearchResponse(query=query, results=[], search_time_ms=elapsed_ms, error=f"搜索失败: {str(exc)}")


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
    agent_cfg = get_agent_config()
    llm = _resolve_llm()

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
            "model": llm["model"],
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        if llm["supports_response_format"]:
            request_body["response_format"] = {"type": "json_object"}

        async with get_http_client(timeout=30.0) as client:
            resp = await client.post(
                f"{llm['base']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm['key']}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)

        elapsed = time.perf_counter() - start
        record_llm_call("rewrite", elapsed)
        usage = data.get("usage", {})
        record_tokens(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

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
        return RewrittenQuery(original=user_query, rewritten=user_query)


# ============================================================
# Tool 3: LLM 相关性打分
# ============================================================
RELEVANCE_SYSTEM_PROMPT = """你是一个搜索结果质量评估专家。对每条搜索结果与用户问题的相关性打分 (0-1)。

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
    if not search_results:
        return RelevanceScores(results=[])

    agent_cfg = get_agent_config()
    llm = _resolve_llm()

    candidates = [
        {"index": i, "url": r.url, "title": r.title, "content": r.content[:500]}
        for i, r in enumerate(search_results)
    ]
    user_message = f"""## 用户问题
{user_query}

## 搜索结果候选
{json.dumps(candidates, ensure_ascii=False, indent=2)}

请为每条结果打分:"""

    try:
        request_body = {
            "model": llm["model"],
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        if llm["supports_response_format"]:
            request_body["response_format"] = {"type": "json_object"}

        async with get_http_client(timeout=30.0) as client:
            resp = await client.post(
                f"{llm['base']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm['key']}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            parsed = json.loads(data["choices"][0]["message"]["content"])

        scores = [
            RelevanceScore(
                url=item.get("url", ""), title=item.get("title", ""),
                relevance=float(item.get("relevance", 0.0)),
                reason=item.get("reason", ""),
            )
            for item in parsed.get("results", [])
        ]
        return RelevanceScores(results=scores)
    except Exception as exc:
        if agent_cfg.verbose:
            print(f"[相关性打分失败] {exc}, 回退到原始分数")
        return RelevanceScores(results=[
            RelevanceScore(
                url=r.url, title=r.title,
                relevance=r.score if r.score else 0.5,
                reason="原始搜索分数",
            )
            for r in search_results
        ])


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
- **中间**: 用无序列表 (- ) 逐条列出关键信息
- **格式**: 使用 Markdown，保持简洁——多用列表，少用段落"""


async def generate_fallback_answer(
    user_query: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> str:
    llm = _resolve_llm()

    messages = [{"role": "system", "content": FALLBACK_SYSTEM_PROMPT}]
    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_query})

    start = time.perf_counter()
    try:
        async with get_http_client(timeout=60.0) as client:
            resp = await client.post(
                f"{llm['base']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm['key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm["model"],
                    "temperature": 0.5,
                    "messages": messages,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"]

        elapsed = time.perf_counter() - start
        record_llm_call("fallback", elapsed)
        record_error("fallback_triggered")
        usage = data.get("usage", {})
        record_tokens(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return answer
    except Exception as exc:
        elapsed = time.perf_counter() - start
        record_llm_call("fallback", elapsed)
        record_error("llm_error")
        return f"抱歉，当前无法生成回答。请稍后重试。（错误: {str(exc)}）"


# ============================================================
# Tool 5: 组合工具 - 深度检索流水线（动态预算控制）
# ============================================================
async def search_and_filter_pipeline(
    query: str,
    deduplicator: Optional[URLDeduplicator] = None,
    use_llm_scoring: bool = False,
    search_depth: Optional[str] = None,
    max_results: Optional[int] = None,
) -> tuple[list[SearchResult], list[tuple[str, str, float]]]:
    agent_cfg = get_agent_config()
    if deduplicator is None:
        deduplicator = URLDeduplicator(window_size=agent_cfg.dedup_window)

    # Step 1: 多查询拆解与并发搜索
    rewritten = await rewrite_query(query)
    search_queries = [rewritten.rewritten] + (rewritten.sub_queries or [])
    search_queries = list(dict.fromkeys(search_queries))[:3]

    effective_max = max_results if max_results is not None else 3
    search_tasks = [
        tavily_search(q, max_results=effective_max, search_depth=search_depth)
        for q in search_queries
    ]
    responses = await asyncio.gather(*search_tasks)

    all_results = []
    for resp in responses:
        if not resp.error:
            all_results.extend(resp.results)

    # Step 2: URL 去重
    deduped: list[SearchResult] = []
    for r in all_results:
        if not deduplicator.is_duplicate(r.url):
            deduped.append(r)
            deduplicator.add(r.url)

    if not deduped:
        return [], []

    # Step 3: 流式网页精读抓取
    urls_to_fetch = [r.url for r in deduped[:4]]
    title_map: dict[str, str] = {r.url: r.title for r in deduped}
    all_chunks: list[dict] = []

    if agent_cfg.verbose:
        print(f"[Pipeline] 开始流式抓取 {len(urls_to_fetch)} 个网页，时间预算 3.0s...")

    async for url, content in stream_extract_contents(urls_to_fetch, time_budget=3.0):
        for c in chunk_text(content, chunk_size=600, overlap=100):
            all_chunks.append({
                "url": url,
                "title": title_map.get(url, ""),
                "text": c,
            })

    if not all_chunks:
        if agent_cfg.verbose:
            print("[Pipeline] 网页抓取全部失败，降级使用搜索引擎 Snippet")
        for r in deduped:
            all_chunks.append({
                "url": r.url, "title": r.title,
                "text": r.content, "score": r.score or 0.5,
            })
        top_chunks = all_chunks[: agent_cfg.top_k_fragments]
    else:
        if agent_cfg.verbose:
            print(f"[Pipeline] 收集到 {len(all_chunks)} 个段落，开始 Rerank 重排...")
        top_chunks = rerank_chunks(query, all_chunks, top_k=agent_cfg.top_k_fragments)

    # Step 5: DeepSeek 级上下文组装（限制 Token 总预算）
    fragments: list[tuple[str, str, float]] = []
    total_tokens = 0
    MAX_CONTEXT_TOKENS = 6000  # 大模型的上下文预算

    for c in top_chunks:
        # 先对每一个网页做单篇截断，防止某一篇极长把总额度占满
        safe_text = smart_truncate_by_token(c["text"], max_tokens=2500)
        chunk_tokens = count_tokens_extractor(safe_text)

        # 如果加上这篇就超预算了，直接丢弃后续的
        if total_tokens + chunk_tokens > MAX_CONTEXT_TOKENS:
            if agent_cfg.verbose:
                print(f"[Pipeline] 上下文预算超限，丢弃剩余段落。当前已使用 {total_tokens} tokens")
            break

        fragments.append((c["url"], safe_text, c.get("score", 0.5)))
        total_tokens += chunk_tokens

    if agent_cfg.verbose:
        print(f"[Pipeline] 检索完成，注入 {len(fragments)} 个段落，总计约 {total_tokens} tokens。")

    return deduped, fragments