"""
智能搜索助手 — FastAPI 后端（兼容前端自定义 API Key 配置版）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

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
    auth_token = base64.b64encode(
        f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()
    ).decode()
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

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from openinference.instrumentation.langchain import LangChainInstrumentor
# ============================================================

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from langchain.mcp import MCPAdapter
    _HAS_MCP = True
except ImportError as e:
    print(f"[警告] langchain.mcp 加载失败，将跳过 MCP 功能。报错详情: {e}")
    MCPAdapter = None
    _HAS_MCP = False

import agent.react as react_module
from agent.graph import SearchAgent
from agent.streaming import run_stream_sse
from agent.models import (
    ProviderOut, ProviderCreate, ProviderUpdate, ProviderModelOut,
)
from config import (
    get_api_config, get_agent_config, get_tdai_config, resolve_llm_connection,
    get_provider_store, ProviderConfig, ProviderModel as PConfigModel,
)
from memory.session_store import get_session_store
from memory.user_store import get_user_store
from memory.memory_manager import get_memory_manager
from memory.tdai_adapter import get_tdai_adapter
from utils.helpers import generate_session_id
from utils.http_client import set_shared_client, reset_shared_client
from utils.metrics import (
    record_http_request, record_sse_stream, update_active_sessions,
    get_metrics_text, get_all_stats,
)
from utils.semantic_cache import get_semantic_cache

import httpx

app = FastAPI(
    title="智能搜索助手 API",
    description="实时联网搜索 Agent 的 FastAPI 后端服务（兼容自定义配置）",
    version="1.2.0",
)

FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
LangChainInstrumentor().instrument()

# ============================================================
# CORS
# ============================================================
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env:
    _allow_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _allow_origins = [
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:3001", "http://127.0.0.1:3001",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 指标中间件
# ============================================================
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

# ============================================================
# 后台会话清理任务
# ============================================================
_cleanup_task: Optional[asyncio.Task] = None
_SESSION_CLEANUP_INTERVAL = 3600.0

async def _session_cleanup_loop(interval: float = _SESSION_CLEANUP_INTERVAL) -> None:
    store = get_session_store()
    while True:
        try:
            await asyncio.sleep(interval)
            removed = store.cleanup_expired()
            if removed:
                print(f"[SessionCleanup] 清理 {removed} 个过期会话")
        except asyncio.CancelledError:
            print("[SessionCleanup] 后台清理任务已取消")
            break
        except Exception as e:
            print(f"[SessionCleanup] 异常: {e}")

# ============================================================
# 启动 / 关闭
# ============================================================
@app.on_event("startup")
async def startup():
    global _cleanup_task

    client = httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=30.0,
        ),
    )
    set_shared_client(client)

    if _HAS_MCP and MCPAdapter is not None:
        try:
            print("[MCP] 正在连接本地默认 MCP Server...")
            async with MCPAdapter({
                "mcpServers": {
                    "weather_server": {
                        "command": sys.executable,
                        "args": ["/app/mcp_server.py"],
                    }
                }
            }) as adapter:
                tools = await adapter.get_tools()
            react_module._GLOBAL_MCP_TOOLS = tools
            print(f"[MCP] 成功加载 {len(tools)} 个默认工具: {[t.name for t in tools]}")
        except Exception as e:
            print(f"[MCP] 加载失败: {e}")
    else:
        print("[MCP] 因依赖缺失，跳过 MCP 初始化。系统继续以无 MCP 模式启动。")

    try:
        get_agent()
        print("[Startup] Agent 单例已预热")
    except Exception as e:
        print(f"[Startup] Agent 预热失败（不影响启动，首个请求会重试）: {e}")

    _cleanup_task = asyncio.create_task(_session_cleanup_loop())
    print(f"[SessionCleanup] 后台清理任务已启动，间隔 {_SESSION_CLEANUP_INTERVAL}s")

@app.on_event("shutdown")
async def shutdown():
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
    reset_shared_client()

# ============================================================
# Agent 单例
# ============================================================
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

    provider: Optional[str] = Field(None, description="Provider ID")
    model: Optional[str] = Field(None, description="模型 ID")
    
    # ★ 核心兼容：接收前端自定义配置
    api_base: Optional[str] = Field(None, description="自定义 API Base URL")
    api_key: Optional[str] = Field(None, description="自定义 API Key")
    
    mcp_config: Optional[dict] = Field(None, description="自定义 MCP 配置")
    enable_deep_think: Optional[bool] = Field(True, description="是否开启深度思考")
    enable_search: Optional[bool] = Field(True, description="是否开启联网搜索")

    fallback_provider: Optional[str] = Field(None, description="降级 Provider ID")
    fallback_model: Optional[str] = Field(None, description="降级模型 ID")
    fallback_api_base: Optional[str] = Field(None, description="降级 API Base URL")
    fallback_api_key: Optional[str] = Field(None, description="降级 API Key")

    search_depth: Optional[str] = Field(None, description="搜索深度 (basic | advanced)")
    top_k: Optional[int] = Field(None, ge=2, le=10, description="保留 Top-K 片段")

class SessionInfo(BaseModel):
    session_id: str
    history: list[dict]
    active: bool

class ConfigDefaults(BaseModel):
    default_provider: Optional[str] = None
    default_model: Optional[str] = None
    search_depth: str
    top_k: int

class HealthResponse(BaseModel):
    status: str
    active_sessions: int

# ============================================================
# Provider CRUD
# ============================================================
@app.get("/api/providers", response_model=list[ProviderOut])
async def list_providers():
    store = get_provider_store()
    return [
        ProviderOut(
            id=p.id, label=p.label, base_url=p.base_url, api_key_env=p.api_key_env,
            api=p.api, models=[ProviderModelOut(id=m.id, label=m.label) for m in p.models],
            has_api_key=p.has_api_key(),
        )
        for p in store.list_providers()
    ]

@app.post("/api/providers", response_model=ProviderOut, status_code=201)
async def create_provider(body: ProviderCreate):
    store = get_provider_store()
    if store.get(body.id) is not None:
        raise HTTPException(409, f"Provider {body.id} 已存在")
    cfg = ProviderConfig(
        id=body.id, label=body.label, base_url=body.base_url, api_key_env=body.api_key_env,
        api=body.api, models=[PConfigModel(id=m.id, label=m.label) for m in body.models],
    )
    store.upsert(cfg)
    return ProviderOut(
        id=cfg.id, label=cfg.label, base_url=cfg.base_url, api_key_env=cfg.api_key_env,
        api=cfg.api, models=[ProviderModelOut(id=m.id, label=m.label) for m in cfg.models],
        has_api_key=cfg.has_api_key(),
    )

@app.patch("/api/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: str, body: ProviderUpdate):
    store = get_provider_store()
    existing = store.get(provider_id)
    if existing is None:
        raise HTTPException(404, f"Provider {provider_id} 不存在")
    data = existing.model_dump()
    for fld in ("label", "base_url", "api_key_env", "api"):
        val = getattr(body, fld, None)
        if val is not None:
            data[fld] = val
    if body.models is not None:
        data["models"] = [m.model_dump() for m in body.models]
    cfg = ProviderConfig(**data)
    store.upsert(cfg)
    return ProviderOut(
        id=cfg.id, label=cfg.label, base_url=cfg.base_url, api_key_env=cfg.api_key_env,
        api=cfg.api, models=[ProviderModelOut(id=m.id, label=m.label) for m in cfg.models],
        has_api_key=cfg.has_api_key(),
    )

@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str):
    store = get_provider_store()
    if not store.delete(provider_id):
        raise HTTPException(404, f"Provider {provider_id} 不存在")
    return {"ok": True, "deleted": provider_id}

# ============================================================
# SSE 聊天端点
# ============================================================
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    span = trace.get_current_span()
    base_tags = ["qa-sysyywe", "chat-stream"]

    if span.is_recording():
        if req.user_id:
            span.set_attribute("langfuse.user.id", req.user_id)
        span.set_attribute("langfuse.session.id", req.session_id or "auto-generated")
        span.set_attribute("langfuse.tags", base_tags)

    agent = get_agent()
    sid = req.session_id or generate_session_id()

    # ★ 核心兼容逻辑：如果前端传了 api_key，优先使用前端配置；否则查 providers.yaml
    try:
        if req.api_key and req.api_base:
            llm_conn = {
                "base": req.api_base,
                "api_key": req.api_key,
                "model": req.model or "qwen-max",
            }
        else:
            llm_conn = resolve_llm_connection(req.provider, req.model)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 降级连接兼容
    fallback_conn = None
    if req.fallback_api_key and req.fallback_api_base:
        fallback_conn = {
            "base": req.fallback_api_base,
            "api_key": req.fallback_api_key,
            "model": req.fallback_model or "qwen-turbo",
        }
    elif req.fallback_provider:
        try:
            fallback_conn = resolve_llm_connection(req.fallback_provider, req.fallback_model)
        except ValueError:
            pass

    record_sse_stream("started")
    disconnected = False

    async def generate():
        nonlocal disconnected
        has_revised = False
        has_fallback = False
        has_error = False
        full_answer = ""
        collected_sources: list[dict] = []

        # 1. 判断是否新会话
        is_new_turn = len(agent.get_history(sid)) == 0

        # 2. 语义缓存查询
        if is_new_turn:
            cache = get_semantic_cache()
            cached = cache.get(req.query, user_id=req.user_id)
            if cached:
                cached_answer, cached_sources = cached
                cache_start = time.perf_counter()
                if span.is_recording():
                    span.set_attribute("langfuse.tags", base_tags + ["semantic_cache_hit"])
                yield _sse_event("progress", {"node": "cache", "message": "命中语义缓存，极速返回..."}).encode("utf-8")

                chunk_size = 5
                for i in range(0, len(cached_answer), chunk_size):
                    yield _sse_event("token", {"text": cached_answer[i : i + chunk_size]}).encode("utf-8")
                    await asyncio.sleep(0.01)

                cache_latency_ms = round((time.perf_counter() - cache_start) * 1000, 2)
                yield _sse_event("sources", {"sources": cached_sources or []}).encode("utf-8")
                yield _sse_event("done", {
                    "confidence": 1.0, "latency_ms": cache_latency_ms,
                    "tokens_used": 0, "is_fallback": False, "cached": True,
                }).encode("utf-8")
                record_sse_stream("completed")
                return

        # 3. 正常 Agent 流程
        async for sse_data in run_stream_sse(
            agent=agent,
            user_query=req.query,
            session_id=sid,
            user_id=req.user_id,
            model_override=llm_conn["model"],
            api_base=llm_conn["base"],
            api_key=llm_conn["api_key"],
            mcp_config=req.mcp_config,
            enable_deep_think=req.enable_deep_think,
            enable_search=req.enable_search,
            fallback_model=fallback_conn["model"] if fallback_conn else None,
            fallback_api_base=fallback_conn["base"] if fallback_conn else None,
            fallback_api_key=fallback_conn["api_key"] if fallback_conn else None,
        ):
            if await request.is_disconnected():
                disconnected = True
                break

            if sse_data.startswith("event: token"):
                try:
                    full_answer += json.loads(sse_data.split("data: ")[1].strip()).get("text", "")
                except Exception:
                    pass
            elif sse_data.startswith("event: reset_tokens"):
                full_answer = ""
            elif sse_data.startswith("event: sources"):
                try:
                    collected_sources = json.loads(sse_data.split("data: ")[1].strip()).get("sources", []) or []
                except Exception:
                    pass
            elif sse_data.startswith("event: revise"):
                try:
                    full_answer = json.loads(sse_data.split("data: ")[1].strip()).get("text", "")
                    has_revised = True
                except Exception:
                    pass
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

        if span.is_recording():
            updated_tags = list(base_tags)
            if has_revised: updated_tags.append("reviewer_revise")
            if has_fallback: updated_tags.append("fallback")
            if has_error: updated_tags.append("error")
            if len(updated_tags) > len(base_tags):
                span.set_attribute("langfuse.tags", updated_tags)

        if is_new_turn and full_answer and not has_error and not has_fallback:
            cache = get_semantic_cache()
            cache.set(req.query, full_answer, user_id=req.user_id, sources=collected_sources)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

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
    agent = get_agent()
    if session_id in agent._sessions:
        history = agent.get_history(session_id)
        return SessionInfo(session_id=session_id, history=history, active=True)
    if user_id:
        store = get_user_store()
        history = store.get_history(user_id, session_id)
        if history:
            return SessionInfo(session_id=session_id, history=history, active=False)
    return SessionInfo(
        session_id=session_id,
        history=[{"role": "assistant", "content": "⚠️ 该会话的上下文记录已过期或已清理。您可以继续在此提问，或者开启一个新的会话。"}],
        active=False,
    )

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
    eps = mm.episodes.list_episodes(user_id)
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
# 配置与健康检查端点
# ============================================================
@app.get("/api/config/defaults")
async def get_config_defaults():
    agent_cfg = get_agent_config()
    store = get_provider_store()
    providers = store.list_providers()
    if providers:
        default_provider = providers[0].id
        default_model = providers[0].models[0].id if providers[0].models else None
    else:
        cfg = get_api_config()
        default_provider = cfg.llm_provider or None
        default_model = cfg.llm_model or None
    return ConfigDefaults(
        default_provider=default_provider,
        default_model=default_model,
        search_depth=agent_cfg.search_depth,
        top_k=agent_cfg.top_k_fragments,
    )

@app.get("/api/health")
async def health():
    agent = get_agent()
    active = len(agent._sessions)
    update_active_sessions(active)
    return HealthResponse(status="ok", active_sessions=active)

@app.get("/api/metrics")
async def metrics(format: Optional[str] = None):
    if format == "json":
        return JSONResponse(content=get_all_stats())
    if format == "availability":
        from utils.metrics import get_availability_stats
        return JSONResponse(content=get_availability_stats())
    return Response(content=get_metrics_text(), media_type="text/plain; charset=utf-8")

# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)