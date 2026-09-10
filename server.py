"""
智能搜索助手 — FastAPI 后端
提供 SSE 流式聊天端点 + 会话管理 REST API
支持 Prometheus 指标收集与 95% SLA 可用性统计
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from agent.graph import SearchAgent
from agent.streaming import run_stream_sse
from config import get_api_config, get_agent_config, get_tdai_config
from memory.session_store import get_session_store
from memory.memory_manager import get_memory_manager
from memory.tdai_adapter import get_tdai_adapter
from utils.helpers import generate_session_id
from utils.http_client import set_shared_client, reset_shared_client
from utils.metrics import (
    record_http_request,
    record_sse_stream,
    update_active_sessions,
    get_metrics_text,
    get_all_stats,
)

import httpx

app = FastAPI(
    title="智能搜索助手 API",
    description="实时联网搜索 Agent 的 FastAPI 后端服务",
    version="1.0.0",
)

# ── CORS ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "http://frontend:3000",   # Docker 内部网络
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── HTTP 指标中间件: 记录所有请求的计数 / 延迟 / 状态码 ──
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Prometheus 指标中间件: 记录每个请求的延迟与结果"""
    start = time.perf_counter()
    status_code = 500
    is_error = False
    try:
        response = await call_next(request)
        status_code = response.status_code
        is_error = status_code >= 400
        return response
    except Exception:
        is_error = True
        raise
    finally:
        duration = time.perf_counter() - start
        path = request.url.path
        # 跳过 /metrics 自身以避免递归
        if path != "/api/metrics":
            record_http_request(
                method=request.method,
                path=path,
                status_code=status_code,
                duration=duration,
                is_error=is_error,
            )


# ── 生命周期: 共享 HTTP 客户端 (连接池复用, 消除 TCP/TLS 握手延迟) ──
@app.on_event("startup")
async def startup():
    """创建全局 httpx AsyncClient，复用 TCP 连接池"""
    import httpx
    client = httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=30.0,
        ),
    )
    set_shared_client(client)


@app.on_event("shutdown")
async def shutdown():
    """关闭全局 httpx AsyncClient"""
    reset_shared_client()

# ── 全局单例 ──
_agent: Optional[SearchAgent] = None


def get_agent() -> SearchAgent:
    global _agent
    if _agent is None:
        _agent = SearchAgent()
    return _agent


# ============================================================
# Pydantic 模型
# ============================================================
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户问题")
    session_id: Optional[str] = Field(None, description="会话 ID (不传则自动生成)")
    user_id: Optional[str] = Field(None, description="用户 ID (用于跨会话记忆归属)")
    model: Optional[str] = Field(None, description="LLM 模型覆盖 (e.g. gpt-4o)")
    search_depth: Optional[str] = Field(None, description="搜索深度 (basic | advanced)")
    top_k: Optional[int] = Field(None, ge=2, le=10, description="保留 Top-K 片段")


class SessionInfo(BaseModel):
    session_id: str
    history: list[dict]
    active: bool


class ConfigDefaults(BaseModel):
    model: str
    search_depth: str
    top_k: int
    llm_api_base: str


class HealthResponse(BaseModel):
    status: str
    active_sessions: int


# ============================================================
# SSE 聊天端点
# ============================================================
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """SSE 流式聊天 — 逐 token 推送答案生成进度"""
    agent = get_agent()
    sid = req.session_id or generate_session_id()

    record_sse_stream("started")
    disconnected = False

    async def generate():
        nonlocal disconnected
        async for sse_data in run_stream_sse(
            agent=agent,
            user_query=req.query,
            session_id=sid,
            user_id=req.user_id,
            model_override=req.model,
            search_depth_override=req.search_depth,
            top_k_override=req.top_k,
        ):
            # 客户端断开时停止
            if await request.is_disconnected():
                disconnected = True
                break
            yield sse_data.encode("utf-8")
            await asyncio.sleep(0)

        if disconnected:
            record_sse_stream("disconnected")
        else:
            record_sse_stream("completed")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# 会话管理端点
# ============================================================
@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    agent = get_agent()
    history = agent.get_history(session_id)
    active = session_id in agent._sessions
    return SessionInfo(session_id=session_id, history=history, active=active)


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    agent = get_agent()
    agent.clear_session(session_id)
    return {"ok": True}


# ============================================================
# 记忆端点（user_id 贯穿的语义 + 情景记忆观测）
# ============================================================
@app.get("/api/memory/{user_id}")
async def get_memory(user_id: str):
    """查看某用户当前的长期事实与情景记忆片段（观测/调试用）。"""
    mm = get_memory_manager()
    facts = mm.facts.get_facts(user_id)
    episodes = _list_episodes(mm, user_id)
    return {
        "user_id": user_id,
        "facts": facts,
        "episodes": episodes,
        "facts_count": len(facts),
        "episodes_count": len(episodes),
    }


def _list_episodes(mm, user_id: str) -> list[dict]:
    """返回该用户全部情景片段（最近在前），供观测端点使用。"""
    rec = mm.episodes._users.get(user_id)
    eps = list(rec.get("episodes", [])) if rec else []
    return list(reversed(eps))


@app.delete("/api/memory/{user_id}")
async def clear_memory(user_id: str):
    """清空某用户的语义 + 情景记忆（不删会话历史）。"""
    mm = get_memory_manager()
    mm.facts.clear_user(user_id)
    mm.episodes.clear_user(user_id)
    return {"ok": True, "user_id": user_id}


@app.get("/api/memory/tdai/{user_id}")
async def get_tdai_memory(user_id: str, query: Optional[str] = None, limit: int = 10):
    """观测 TencentDB-Agent-Memory 侧车的分层记忆（L1 记忆 + L0 对话）。

    返回侧车健康状态，以及可选的定向检索结果（L1 原子事实/场景 + L0 原始对话）。

    注意：TDAI Gateway 的 /search/memories 与 /search/conversations 为**全局检索**，
    不按 user_id 过滤（user_id 仅作用于 recall/capture 的 L3 人设隔离）。此端点的
    user_id 仅作语义归属标记，检索结果面向全局；`query` 用于定向检索，缺省时不检索。
    """
    tdai_cfg = get_tdai_config()
    if not tdai_cfg.tdai_enabled:
        return {
            "user_id": user_id,
            "enabled": False,
            "message": "TDAI 未启用（TDAI_ENABLED=false），无侧车记忆可观测",
        }

    adapter = get_tdai_adapter()
    healthy = await adapter.health()

    query = (query or "").strip()
    limit = max(1, min(limit, 100))

    memories = await adapter.search_memories(query, limit=limit) if query else {}
    conversations = await adapter.search_conversations(query, limit=limit) if query else {}

    return {
        "user_id": user_id,
        "enabled": True,
        "gateway_healthy": healthy,
        "query": query,
        "memories": memories,              # L1 原子事实/场景（全局检索）
        "conversations": conversations,    # L0 原始对话（全局检索）
    }


# ============================================================
# 配置端点
# ============================================================
@app.get("/api/config/defaults")
async def get_config_defaults():
    api_cfg = get_api_config()
    agent_cfg = get_agent_config()
    return ConfigDefaults(
        model=api_cfg.llm_model,
        search_depth=agent_cfg.search_depth,
        top_k=agent_cfg.top_k_fragments,
        llm_api_base=api_cfg.llm_api_base,
    )


# ============================================================
# 健康检查
# ============================================================
@app.get("/api/health")
async def health():
    agent = get_agent()
    active = len(agent._sessions)
    update_active_sessions(active)
    return HealthResponse(
        status="ok",
        active_sessions=active,
    )


# ============================================================
# Prometheus 指标端点
# ============================================================
@app.get("/api/metrics")
async def metrics(format: Optional[str] = None):
    """
    可观测性端点: 返回 Prometheus 文本格式指标或 JSON 可用性统计。

    - 默认 (Accept: text/plain 或无 format) → Prometheus 文本格式
    - ?format=json → JSON 格式 (含可用性统计 + 指标快照)
    - ?format=availability → 仅可用性统计 (95% SLA)
    """
    if format == "json":
        return JSONResponse(content=get_all_stats())
    if format == "availability":
        from utils.metrics import get_availability_stats
        return JSONResponse(content=get_availability_stats())

    # 默认返回 Prometheus 文本格式
    return Response(
        content=get_metrics_text(),
        media_type="text/plain; charset=utf-8",
    )


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
