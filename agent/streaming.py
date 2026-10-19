"""
智能搜索助手 — SSE 流式传输编排器
"""
from __future__ import annotations

import json
import time
from typing import AsyncGenerator, Optional

from langchain_core.messages import ToolMessage

from agent.models import GeneratedAnswer, Source
from config import get_agent_config, get_tdai_config
from memory.user_store import get_user_store
from memory.memory_manager import get_memory_manager
from memory.tdai_adapter import get_tdai_adapter
from utils.helpers import count_tokens, generate_session_id


# ============================================================
# SSE 事件格式化工具
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
    llm_provider: Optional[str] = None,  # ★ 新增
    api_base: Optional[str] = None,      # ★ 新增
    api_key: Optional[str] = None,       # ★ 新增
    search_depth_override: Optional[str] = None,
    top_k_override: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """
    执行 ReAct Agent 并实时推送 SSE 事件。
    """
    agent_cfg = get_agent_config()
    tdai_cfg = get_tdai_config()

    # ★ 移除了全局 api_cfg.llm_model 的覆写，改为安全透传

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
        revised_text = None

        # ★ 核心透传：将模型配置和搜索参数全部传给 ReactAgent
        async for event in agent._react.astream_events(
            user_query,
            history,
            memory_context=memory_context,
            user_id=user_id,
            model_override=model_override,  # ★
            llm_provider=llm_provider,      # ★
            api_base=api_base,              # ★
            api_key=api_key,                # ★
        ):
            kind = event.get("event", "")
            data = event.get("data", {})

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                if getattr(chunk, "tool_call_chunks", None):
                    continue
                text = getattr(chunk, "content", "")
                if text:
                    answer_text += text
                    yield _sse_event("token", {"text": text})

            elif kind == "on_tool_start":
                name = event.get("name", "")
                if "search" in name:
                    answer_text = ""  # 如果中途调用了搜索，清空之前的自言自语
                    tool_input = data.get("input", {}) or {}
                    q = tool_input.get("query", "") or ""
                    yield _sse_event(
                        "progress",
                        {"node": "search", "message": f"正在搜索: {q[:60]}"},
                    )

            elif kind == "on_tool_end":
                new_sources = _extract_sources_from_tool_output(data.get("output"))
                existing = {s.url for s in sources}
                for s in new_sources:
                    if s.url not in existing:
                        existing.add(s.url)
                        sources.append(s)

            # 处理自定义的 progress 事件（如：触发 Reviewer 审核）
            elif kind == "progress":
                yield _sse_event("progress", data)

            # 处理自定义的 revise 事件（Reviewer 发现幻觉并重写了答案）
            elif kind == "revise":
                revised_text = data.get("text", "")
                yield _sse_event("revise", data)

        # ── 结果构造 ──
        elapsed_ms = (time.perf_counter() - start) * 1000

        # 如果 Reviewer 修改了答案，用修复后的版本替换原文本
        if revised_text:
            answer_text = revised_text

        if not answer_text:
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
            if tdai_cfg.tdai_enabled:
                captured = await get_tdai_adapter().capture(
                    user_id, session_id, user_query, answer.answer
                )
                if not captured:
                    await get_memory_manager().aremember(user_id, session_id, "user", user_query)
                    await get_memory_manager().aremember(user_id, session_id, "assistant", answer.answer)
            else:
                await get_memory_manager().aremember(user_id, session_id, "user", user_query)
                await get_memory_manager().aremember(user_id, session_id, "assistant", answer.answer)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        yield _sse_event(
            "error",
            {"message": str(exc), "code": type(exc).__name__},
        )