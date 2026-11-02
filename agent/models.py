"""
智能搜索助手 — 数据模型定义
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TypedDict

from pydantic import BaseModel, Field


# ============================================================
# 搜索相关（不变）
# ============================================================
class SearchResult(BaseModel):
    url: str = Field(...)
    title: str = Field(default="")
    content: str = Field(default="")
    score: float = Field(default=0.0)
    raw_response: Optional[dict] = Field(default=None, repr=False)


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult] = Field(default_factory=list)
    total_count: int = 0
    search_time_ms: float = 0.0
    error: Optional[str] = None


class RelevanceScore(BaseModel):
    url: str
    title: str
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="")


class RelevanceScores(BaseModel):
    results: list[RelevanceScore] = Field(default_factory=list)


# ============================================================
# 查询改写（不变）
# ============================================================
class RewrittenQuery(BaseModel):
    original: str
    rewritten: str
    language: str = "zh"
    intent: str = "factual"
    sub_queries: list[str] = Field(default_factory=list)


# ============================================================
# 答案生成（不变）
# ============================================================
class Source(BaseModel):
    url: str
    title: str
    snippet: str = Field(default="")


class GeneratedAnswer(BaseModel):
    query: str
    answer: str
    sources: list[Source] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_fallback: bool = False
    tokens_used: int = 0
    latency_ms: float = 0.0


# ============================================================
# Agent 状态图（不变）
# ============================================================
class AgentState(TypedDict, total=False):
    session_id: str
    user_query: str
    user_query_raw: str
    rewritten_queries: list[RewrittenQuery]
    search_results: list[SearchResult]
    deduped_results: list[SearchResult]
    relevance_scores: RelevanceScores
    top_results: list[SearchResult]
    final_answer: GeneratedAnswer
    history: list[dict[str, str]]
    error: Optional[str]
    retry_count: int
    fallback_triggered: bool
    search_failed: bool
    started_at: str
    completed_at: str


# ============================================================
# 工具元数据（不变）
# ============================================================
class ToolCategory(str, Enum):
    SEARCH = "search"
    REWRITE = "rewrite"
    RELEVANCE = "relevance"
    FALLBACK = "fallback"


@dataclass
class ToolMetadata:
    name: str
    category: ToolCategory
    description: str
    version: str = "1.0.0"


# ============================================================
# 会话与缓存（不变）
# ============================================================
@dataclass
class SessionEntry:
    session_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    last_query: str = ""
    created_at: float = 0.0
    last_access: float = 0.0

    def touch(self, now: float) -> None:
        self.last_access = now


# ============================================================
# 限流（不变）
# ============================================================
@dataclass
class RateLimitState:
    request_timestamps: list[float] = field(default_factory=list)
    search_timestamps: list[float] = field(default_factory=list)


# ============================================================
# 历史会话检索（不变）
# ============================================================
class HistoryMessage(BaseModel):
    session_id: str
    role: str
    content: str


class HistoryRecall(BaseModel):
    query: str
    count: int = 0
    sessions: list[HistoryMessage] = Field(default_factory=list)


# ============================================================
# Provider API 模型（新增）
# ============================================================
class ProviderModelOut(BaseModel):
    id: str
    label: str = ""


class ProviderOut(BaseModel):
    id: str
    label: str = ""
    base_url: str
    api_key_env: str
    api: str = "openai-completions"
    models: list[ProviderModelOut] = Field(default_factory=list)
    # ★ 注意：绝不返回 api_key 明文，只返回 api_key_env 名称
    has_api_key: bool = False


class ProviderCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    label: str = ""
    base_url: str = Field(..., min_length=1)
    api_key_env: str = Field(..., min_length=1, description="环境变量名，如 DEEPSEEK_API_KEY")
    api: str = "openai-completions"
    models: list[ProviderModelOut] = Field(default_factory=list)


class ProviderUpdate(BaseModel):
    label: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api: Optional[str] = None
    models: Optional[list[ProviderModelOut]] = None


# ============================================================
# 修改后的 ChatRequest（★ 移除 api_base / api_key / llm_provider）
# ============================================================
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    user_id: Optional[str] = None

    # ★ 新增：前端只传 provider + model
    provider: Optional[str] = Field(None, description="Provider ID，如 deepseek-official")
    model: Optional[str] = Field(None, description="模型 ID，如 deepseek-chat")

    mcp_config: Optional[dict] = None
    enable_deep_think: Optional[bool] = True
    enable_search: Optional[bool] = True

    fallback_provider: Optional[str] = Field(None, description="降级 Provider ID")
    fallback_model: Optional[str] = Field(None, description="降级模型 ID")

    search_depth: Optional[str] = None
    top_k: Optional[int] = Field(None, ge=2, le=10)