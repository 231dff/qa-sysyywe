"""
智能搜索助手 — FastAPI 后端
提供 SSE 流式聊天端点 + 会话管理 REST API
支持 Prometheus 指标收集与 95% SLA 可用性统计
集成 OpenTelemetry + Langfuse 全链路 Trace
支持语义缓存（Semantic Cache）极速响应
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import base64
import json
from pathlib import Path

# ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ============================================================
# OpenTelemetry 初始化 (必须在其他 import 之前)
# ============================================================
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "")

if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY and LANGFUSE_HOST:
    auth_token = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
    
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{LANGFUSE_HOST}/api/public/otel"
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth_token}"
    
    resource = Resource.create({SERVICE_NAME: "qa-sysyywe-backend"})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    print(f"[OTel] Tracing enabled, exporting to {LANGFUSE_HOST}")
else:
    print("[OTel] Tracing disabled (missing LANGFUSE env vars)")

# 引入自动埋点 instrumentation
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from openinference.instrumentation.langchain import LangChainInstrumentor
# ============================================================

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from agent.graph import SearchAgent
from agent.streaming import run_stream_sse
from config import get_api_config, get_agent_config, get_tdai_config, resolve_llm_connection
from memory.session_store import get_session_store
from memory.user_store import get_user_store
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
from utils.semantic_cache import get_semantic_cache  # ★ 引入语义缓存

import httpx

app = FastAPI(
    title="智能搜索助手 API",
    description="实时联网搜索 Agent 的 FastAPI 后端服务",
    version="1.0.0",
)

# 开启自动化埋点
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
LangChainInstrumentor().instrument()

# ── CORS ──
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env:
    _allow_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _allow_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
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
        if path != "/api/metrics":
            record_http_request(
                method=request.method,
                path=path,
                status_code=status_code,
                duration=duration,
                is_error=is_error,
            )


@app.on_event("startup")
async def startup():
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
    
    # 支持前端动态下发大模型配置
    model: Optional[str] = Field(None, description="LLM 模型覆盖")
    llm_provider: Optional[str] = Field(None, description="提供商标识 (如 openai/qwen)")
    api_base: Optional[str] = Field(None, description="LLM API Base URL")
    api_key: Optional[str] = Field(None, description="LLM API Key")

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
    span = trace.get_current_span()
    base_tags = ["qa-sysyywe", "chat-stream"]
    
    if span.is_recording():
        if req.user_id:
            span.set_attribute("langfuse.user.id", req.user_id)
        sid_for_trace = req.session_id or "auto-generated"
        span.set_attribute("langfuse.session.id", sid_for_trace)
        span.set_attribute("langfuse.tags", base_tags)

    agent = get_agent()
    sid = req.session_id or generate_session_id()

    record_sse_stream("started")
    disconnected = False

    async def generate():
        nonlocal disconnected
        has_revised = False
        has_fallback = False
        has_error = False
        full_answer = ""
        
        # ★ 1. 判断是否是新会话（没有历史上下文）
        is_new_turn = len(agent.get_history(sid)) == 0
        
        # ★ 2. 尝试从语义缓存获取（仅在新会话时生效，防止上下文污染）
        if is_new_turn:
            cache = get_semantic_cache()
            cached_answer = cache.get(req.query)
            if cached_answer:
                if span.is_recording():
                    span.set_attribute("langfuse.tags", base_tags + ["semantic_cache_hit"])
                yield _sse_event("progress", {"node": "cache", "message": "命中语义缓存，极速返回..."}).encode("utf-8")
                
                # 模拟打字机效果极速输出
                chunk_size = 5
                for i in range(0, len(cached_answer), chunk_size):
                    yield _sse_event("token", {"text": cached_answer[i:i+chunk_size]}).encode("utf-8")
                    await asyncio.sleep(0.01)
                
                yield _sse_event("sources", {"sources": []}).encode("utf-8")
                yield _sse_event("done", {"confidence": 1.0, "latency_ms": 10.0, "tokens_used": 0, "is_fallback": False}).encode("utf-8")
                return

        # 3. 正常走 Agent 流程
        async for sse_data in run_stream_sse(
            agent=agent,
            user_query=req.query,
            session_id=sid,
            user_id=req.user_id,
            model_override=req.model,
            llm_provider=req.llm_provider,
            api_base=req.api_base,
            api_key=req.api_key,
            search_depth_override=req.search_depth,
            top_k_override=req.top_k,
        ):
            if await request.is_disconnected():
                disconnected = True
                break
            
            # 收集完整文本，用于写入缓存
            if sse_data.startswith("event: token"):
                try:
                    data_str = sse_data.split("data: ")[1].strip()
                    full_answer += json.loads(data_str).get("text", "")
                except: pass
            elif sse_data.startswith("event: revise"):
                try:
                    data_str = sse_data.split("data: ")[1].strip()
                    full_answer = json.loads(data_str).get("text", "")
                    has_revised = True
                except: pass
            elif '"is_fallback": true' in sse_data or '"is_fallback":true' in sse_data:
                has_fallback = True
            elif sse_data.startswith("event: error"):
                has_error = True

            yield sse_data.encode("utf-8")
            await asyncio.sleep(0)

        if disconnected:
            record_sse_stream("disconnected")
        else:
            record_sse_stream("completed")

        # 4. 将失败状态写入 Trace Tags
        if span.is_recording():
            updated_tags = list(base_tags)
            if has_revised: updated_tags.append("reviewer_revise")
            if has_fallback: updated_tags.append("fallback")
            if has_error: updated_tags.append("error")
            
            if len(updated_tags) > len(base_tags):
                span.set_attribute("langfuse.tags", updated_tags)

        # ★ 5. 生成成功，写入语义缓存
        if is_new_turn and full_answer and not has_error and not has_fallback:
            cache = get_semantic_cache()
            cache.set(req.query, full_answer)

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
# 辅助 SSE 格式化 (如果没有引入的话，直接在这个文件里定义)
# ============================================================
def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


# ============================================================
# 会话管理端点
# ============================================================

@app.get("/api/sessions")
async def list_sessions(user_id: str = ""):
    if not user_id:
        return {"user_id": "", "sessions": []}

    store = get_user_store()
    sids = store.list_sessions(user_id)

    result = []
    for sid in sids:
        history = store.get_history(user_id, sid)
        if not history: continue

        title = "新对话"
        for msg in history:
            if msg.get("role") == "user":
                title = msg["content"][:30]
                break

        last_msg = history[-1] if history else {}

        result.append({
            "session_id": sid,
            "title": title,
            "preview": last_msg.get("content", "")[:60],
            "message_count": len(history),
        })

    result.sort(key=lambda x: x["message_count"], reverse=True)
    return {"user_id": user_id, "sessions": result}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str, user_id: Optional[str] = None):
    if user_id:
        store = get_user_store()
        history = store.get_history(user_id, session_id)
        if history:
            return SessionInfo(session_id=session_id, history=history, active=False)

    agent = get_agent()
    history = agent.get_history(session_id)
    active = session_id in agent._sessions
    
    # 如果历史记录为空，返回友好提示
    if not history:
        history = [{
            "role": "assistant", 
            "content": "⚠️ 该会话的上下文记录已过期或已清理。您可以继续在此提问，或者开启一个新的会话。"
        }]

    return SessionInfo(session_id=session_id, history=history, active=active)


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str, user_id: Optional[str] = None):
    if user_id:
        store = get_user_store()
        store.delete_session(user_id, session_id)

    agent = get_agent()
    agent.clear_session(session_id)
    return {"ok": True}


# ============================================================
# 记忆端点
# ============================================================
@app.get("/api/memory/{user_id}")
async def get_memory(user_id: str):
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
    rec = mm.episodes._users.get(user_id)
    eps = list(rec.get("episodes", [])) if rec else []
    return list(reversed(eps))


@app.delete("/api/memory/{user_id}")
async def clear_memory(user_id: str):
    mm = get_memory_manager()
    mm.facts.clear_user(user_id)
    mm.episodes.clear_user(user_id)
    return {"ok": True, "user_id": user_id}


@app.get("/api/memory/tdai/{user_id}")
async def get_tdai_memory(user_id: str, query: Optional[str] = None, limit: int = 10):
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
        "memories": memories,
        "conversations": conversations,
    }


# ============================================================
# 配置端点
# ============================================================
@app.get("/api/config/defaults")
async def get_config_defaults():
    api_cfg = get_api_config()
    agent_cfg = get_agent_config()
    conn = resolve_llm_connection()
    return ConfigDefaults(
        model=conn["model"],
        search_depth=agent_cfg.search_depth,
        top_k=agent_cfg.top_k_fragments,
        llm_api_base=conn["base"],
    )


# ============================================================
# 健康检查
# ============================================================
@app.get("/api/health")
async def health():
    agent = get_agent()
    active = len(agent._sessions)
    update_active_sessions(active)
    return HealthResponse(status="ok", active_sessions=active)


# ============================================================
# Prometheus 指标端点
# ============================================================
@app.get("/api/metrics")
async def metrics(format: Optional[str] = None):
    if format == "json":
        return JSONResponse(content=get_all_stats())
    if format == "availability":
        from utils.metrics import get_availability_stats
        return JSONResponse(content=get_availability_stats())

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