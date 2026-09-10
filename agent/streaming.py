"""
智能搜索助手 — SSE 流式传输编排器

基于 ReactAgent.astream_events (LangGraph v2) 将 ReAct 执行过程桥接为
前端可消费的 SSE 事件流。事件契约对前端完全向后兼容：

    progress → {node, message}     node ∈ rewrite|search|generate|fallback|score
    token    → {text}
    sources  → {sources:[{url,title,snippet}]}
    done     → {confidence, latency_ms, tokens_used, is_fallback}
    error    → {message, code}
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from langchain_core.messages import ToolMessage

from agent.models import GeneratedAnswer, Source
from config import get_agent_config, get_api_config, get_tdai_config
from memory.user_store import get_user_store
from memory.memory_manager import get_memory_manager
from memory.tdai_adapter import get_tdai_adapter
from utils.helpers import count_tokens, generate_session_id


# ============================================================
# SSE 事件格式工具
# ============================================================
def _sse_event(event: str, data: dict) -> str:
    """将 Python dict 格式化为 SSE 兼容的字节流。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _extract_sources_from_tool_output(output) -> list[Source]:
    """从 on_tool_end 的 output 中提取结构化来源（防御式解析）。"""
    sources: list[Source] = []
    seen: set[str] = set()

    def _consume(payload: dict):
        for item in payload.get("sources", []):
            url = item.get("url", "")
            if url and url not in seen:
                seen.add(url)
                sources.append(Source(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                ))

    content = output
    if isinstance(output, ToolMessage):
        content = output.content

    if isinstance(content, str):
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                _consume(payload)
        except (json.JSONDecodeError, TypeError):
            pass
    elif isinstance(content, dict):
        _consume(content)

    return sources


# ============================================================
# 核心: run_stream_sse — 实时逐 token 推送
# ============================================================
async def run_stream_sse(
    agent,
    user_query: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    model_override: Optional[str] = None,
    search_depth_override: Optional[str] = None,
    top_k_override: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """
    执行 ReAct Agent 并实时推送 SSE 事件。

    agent 为 SearchAgent（内部持有 ReactAgent）。本函数直接消费
    agent._react.astream_events(...) 的事件流，逐 token 推送答案，
    并在结束时维护会话历史。
    """
    agent_cfg = get_agent_config()
    api_cfg = get_api_config()
    tdai_cfg = get_tdai_config()

    # ── 运行时配置覆盖（与旧实现语义一致） ──
    orig_model = api_cfg.llm_model
    if model_override:
        api_cfg.llm_model = model_override

    # ── 会话初始化 ──
    if session_id is None:
        session_id = generate_session_id()
    if session_id not in agent._sessions:
        # 先尝试从持久化存储恢复该用户的会话
        restored = get_user_store().get_history(user_id, session_id) if user_id else []
        agent._sessions[session_id] = restored
    history = agent._sessions[session_id]

    # ── 记忆召回：语义 + 情景（或 TDAI 分层长期记忆），注入为 system 前缀 ──
    if tdai_cfg.tdai_enabled:
        # TDAI 优先：L0→L3 分层记忆召回；Gateway 不可用/无结果时回退自研，避免记忆真空
        memory_context = await get_tdai_adapter().recall(user_id, session_id, user_query)
        if not memory_context:
            memory_ctx = get_memory_manager().recall(user_id, user_query)
            memory_context = memory_ctx.render() if user_id else ""
    else:
        memory_ctx = get_memory_manager().recall(user_id, user_query)
        memory_context = memory_ctx.render() if user_id else ""

    start = time.perf_counter()

    try:
        yield _sse_event(
            "progress",
            {"node": "generate", "message": "正在思考如何回答..."},
        )

        answer_text = ""
        sources: list[Source] = []
        used_search = False

        async for event in agent._react.astream_events(user_query, history, memory_context=memory_context):
            kind = event.get("event", "")
            data = event.get("data", {})

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                # 工具调用回合会携带 tool_call_chunks，其文本是「思考」而非最终答案
                if getattr(chunk, "tool_call_chunks", None):
                    continue
                text = getattr(chunk, "content", "")
                if text:
                    answer_text += text
                    yield _sse_event("token", {"text": text})

            elif kind == "on_tool_start":
                name = event.get("name", "")
                if "search" in name:
                    used_search = True
                    # 丢弃工具调用前可能残留的思考文本
                    answer_text = ""
                    tool_input = data.get("input", {}) or {}
                    q = tool_input.get("query", "") or ""
                    yield _sse_event(
                        "progress",
                        {"node": "search", "message": f"正在搜索: {q[:60]}"},
                    )

            elif kind == "on_tool_end":
                new_sources = _extract_sources_from_tool_output(data.get("output"))
                # 合并去重
                existing = {s.url for s in sources}
                for s in new_sources:
                    if s.url not in existing:
                        existing.add(s.url)
                        sources.append(s)

        # ── 结果构造 ──
        elapsed_ms = (time.perf_counter() - start) * 1000

        if not answer_text:
            # 模型未产出有效文本 → 降级
            fallback_text = await agent._react._fallback(
                user_query, history, start, confidence=0.5
            )
            answer = fallback_text
            yield _sse_event(
                "progress",
                {"node": "fallback", "message": "未获取到有效回答，切换为降级回答..."},
            )
            yield _sse_event("token", {"text": answer.answer})
        else:
            confidence = 0.85 if sources else 0.7
            answer = GeneratedAnswer(
                query=user_query,
                answer=answer_text,
                sources=sources,
                confidence=confidence,
                is_fallback=False,
                tokens_used=count_tokens(answer_text),
                latency_ms=elapsed_ms,
            )

        # 来源
        yield _sse_event(
            "sources",
            {"sources": [
                {"url": s.url, "title": s.title, "snippet": s.snippet}
                for s in answer.sources
            ]},
        )

        # 完成
        yield _sse_event(
            "done",
            {
                "confidence": answer.confidence,
                "latency_ms": answer.latency_ms,
                "tokens_used": answer.tokens_used,
                "is_fallback": answer.is_fallback,
            },
        )

        # ── 更新历史 ──
        history.append({"role": "user", "content": user_query})
        history.append({"role": "assistant", "content": answer.answer})
        max_turns = agent_cfg.max_history_turns
        if len(history) > max_turns * 2:
            agent._sessions[session_id] = history[-(max_turns * 2):]
        else:
            agent._sessions[session_id] = history

        # ── 持久化到用户存储（跨进程/重启恢复） ──
        if user_id:
            get_user_store().set_history(user_id, session_id, agent._sessions[session_id])
            # 记忆写回：TDAI 侧车为一轮 capture；自研为分开的 aremember（情景 + 语义事实）
            if tdai_cfg.tdai_enabled:
                captured = await get_tdai_adapter().capture(
                    user_id, session_id, user_query, answer.answer
                )
                if not captured:
                    # Gateway 捕获失败 → 回退自研，避免记忆丢失
                    await get_memory_manager().aremember(user_id, session_id, "user", user_query)
                    await get_memory_manager().aremember(user_id, session_id, "assistant", answer.answer)
            else:
                await get_memory_manager().aremember(user_id, session_id, "user", user_query)
                await get_memory_manager().aremember(user_id, session_id, "assistant", answer.answer)

    except Exception as exc:
        yield _sse_event(
            "error",
            {"message": str(exc), "code": type(exc).__name__},
        )

    finally:
        api_cfg.llm_model = orig_model
