"""
智能搜索助手 — 工具注册中心 (完整极速版)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import httpx

from agent.models import (
    RelevanceScore, RelevanceScores, RewrittenQuery,
    SearchResponse, SearchResult, ToolCategory, ToolMetadata,
)
from config import get_api_config
from utils.helpers import exponential_backoff
from utils.http_client import get_http_client
from utils.metrics import record_search

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

# ============================================================
# 极简内存缓存 (TTL 60秒)
# ============================================================
_CACHE: dict[str, tuple[float, list[tuple[str, str, float]]]] = {}
_CACHE_TTL = 60.0

def _get_cache(query: str) -> Optional[list[tuple[str, str, float]]]:
    key = query.strip().lower()
    if key in _CACHE:
        ts, data = _CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            return data
        else:
            del _CACHE[key]
    return None

def _set_cache(query: str, data: list[tuple[str, str, float]]):
    if len(_CACHE) > 1000:
        _CACHE.clear()
    _CACHE[query.strip().lower()] = (time.time(), data)


# ============================================================
# 兼容函数（内部不走 LLM，直接透传）
# ============================================================
async def rewrite_query(user_query: str, conversation_history: Optional[list] = None) -> RewrittenQuery:
    """极速模式：直接返回原词，不调 LLM 耗费时间。"""
    return RewrittenQuery(original=user_query, rewritten=user_query, language="zh", intent="factual", sub_queries=[])

async def score_relevance(user_query: str, search_results: list[SearchResult]) -> RelevanceScores:
    """极速模式：直接返回原始分数。"""
    return RelevanceScores(results=[
        RelevanceScore(url=r.url, title=r.title, relevance=r.score if r.score else 0.5, reason="无打分")
        for r in search_results
    ])

async def generate_fallback_answer(user_query: str, conversation_history: Optional[list] = None) -> str:
    """降级兜底文本。"""
    return "抱歉，当前搜索服务异常或网络超时，请稍后重试。"


# ============================================================
# Tavily 搜索
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
    max_results = max_results or 4
    search_depth = search_depth or "advanced"

    payload = {
        "api_key": api_cfg.tavily_api_key.get_secret_value(),
        "query": query, 
        "search_depth": search_depth,
        "max_results": max_results,
        "include_answer": False, 
        "include_raw_content": False,
    }

    start = time.perf_counter()
    try:
        async with get_http_client(timeout=8.0) as client:
            resp = await client.post(
                f"{api_cfg.tavily_api_base}/search",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()

        results = [
            SearchResult(
                url=item.get("url", ""), title=item.get("title", ""),
                content=item.get("content", ""), score=item.get("score", 0.0),
            )
            for item in data.get("results", [])
        ]
        elapsed_ms = (time.perf_counter() - start) * 1000
        record_search(provider="tavily", status="success", duration=elapsed_ms / 1000)
        return SearchResponse(query=query, results=results, total_count=len(results))
    except Exception as exc:
        print(f"[Search] 搜索失败: {exc}")
        return SearchResponse(query=query, results=[], error=str(exc))


# ============================================================
# 核心流水线：直接使用摘要，不抓网页
# ============================================================
async def search_and_filter_pipeline(
    query: str,
    deduplicator: Optional[object] = None,
    use_llm_scoring: bool = False,
    search_depth: Optional[str] = None,
    max_results: Optional[int] = None,
) -> tuple[list[SearchResult], list[tuple[str, str, float]]]:
    
    # 1. 查缓存
    cached = _get_cache(query)
    if cached:
        print(f"[Pipeline] 命中本地缓存: {query[:20]}...")
        return [], cached

    # 2. 直接搜索
    effective_max = max_results if max_results is not None else 4
    resp = await tavily_search(query, max_results=effective_max, search_depth=search_depth)

    if not resp.results:
        return [], []

    # 3. 提取 Tavily 原生摘要 (安全截断 1000 字)
    fragments: list[tuple[str, str, float]] = []
    for r in resp.results:
        safe_text = r.content[:1000] if r.content else ""
        if safe_text:
            fragments.append((r.url, safe_text, r.score))

    # 4. 写缓存
    _set_cache(query, fragments)
    return resp.results, fragments