"""
智能搜索助手 — SSE 流式传输编排器
"""
from __future__ import annotations

import json
import time
import asyncio
from typing import AsyncGenerator, Optional

from langchain_core.messages import ToolMessage

from agent.models import GeneratedAnswer, Source
from config import get_agent_config, get_tdai_config
from memory.user_store import get_user_store
from memory.memory_manager import get_memory_manager
from memory.tdai_adapter import get_tdai_adapter
from utils.helpers import count_tokens, generate_session_id


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _extract_sources_from_tool_output(output) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    def _consume(payload: dict):
        for item in payload.get("sources", []):
            url = item.get("url", "")
            if url and url not in seen:
                seen.add(url)
                sources.append(Source(url=url, title=item.get("title", ""), snippet=item.get("snippet", "")))
    content = output.content if isinstance(output, ToolMessage) else output
    if isinstance(content, str):
        try:
            payload = json.loads(content)
            if isinstance(payload, dict): _consume(payload)
        except (json.JSONDecodeError, TypeError): pass
    elif isinstance(content, dict): _consume(content)
    return sources


async def run_stream_sse(
    agent,
    user_query: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    model_override: Optional[str] = None,
    llm_provider: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    mcp_config: Optional[dict] = None,
    search_depth_override: Optional[str] = None,
    top_k_override: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    agent_cfg = get_agent_config()
    tdai_cfg = get_tdai_config()

    if session_id is None:
        session_id = generate_session_id()
    if session_id not in agent._sessions:
        restored = get_user_store().get_history(user_id, session_id) if user_id else []
        agent._sessions[session_id] = restored
    history = agent._sessions[session_id]

    history.append({"role": "user", "content": user_query})
    agent._sessions[session_id] = history

    start = time.perf_counter()
    answer_text = ""
    sources: list[Source] = []
    saved_to_history = False

    try:
        yield _sse_event("progress", {"node": "generate", "message": "正在思考如何回答..."})

        if tdai_cfg.tdai_enabled:
            memory_context = await get_tdai_adapter().recall(user_id, session_id, user_query)
            if not memory_context:
                memory_context = get_memory_manager().recall(user_id, user_query).render() if user_id else ""
        else:
            memory_context = get_memory_manager().recall(user_id, user_query).render() if user_id else ""

        async for event in agent._react.astream_events(
            user_query,
            history,
            memory_context=memory_context,
            user_id=user_id,
            model_override=model_override,
            llm_provider=llm_provider,
            api_base=api_base,
            api_key=api_key,
            mcp_config=mcp_config,
        ):
            kind = event.get("event", "")
            data = event.get("data", {})

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                if getattr(chunk, "tool_call_chunks", None): continue
                
                # ★ 核心修复：同时提取 Qwen 的 content 和 reasoning_content
                text = getattr(chunk, "content", "") or ""
                
                # Qwen 的思考过程可能藏在 additional_kwargs 或 response_metadata 里
                reasoning = ""
                if hasattr(chunk, "additional_kwargs"):
                    reasoning = chunk.additional_kwargs.get("reasoning_content", "")
                
                if reasoning and not text:
                    # 这是一个有效的思考心跳，可以偷偷发给前端一个空白 token 防止 120s 超时
                    # 或者发一个 progress 状态提示用户。
                    # 这里我们发一个空文本 token 维持 SSE 连接。
                    yield _sse_event("token", {"text": ""}) 
                
                if text:
                    answer_text += text
                    yield _sse_event("token", {"text": text})

            elif kind == "on_tool_start":
                name = event.get("name", "")
                if "search" in name:
                    if answer_text:
                        yield _sse_event("reset_tokens", {})
                    answer_text = ""
                    
                    tool_input = data.get("input", {}) or {}
                    q = tool_input.get("query", "") or ""
                    yield _sse_event("progress", {"node": "search", "message": f"正在深度检索并精读网页: {q[:30]}..."})
                elif "spawn_search_worker" in name:
                    yield _sse_event("progress", {"node": "search", "message": "正在委派子 Agent 进行多维度检索..."})

            elif kind == "on_tool_end":
                name = event.get("name", "")
                if "search" in name or "spawn_search_worker" in name:
                    yield _sse_event("progress", {"node": "generate", "message": "网页精读完毕，正在提炼核心事实并生成回答..."})
                
                for s in _extract_sources_from_tool_output(data.get("output")):
                    if not any(x.url == s.url for x in sources): sources.append(s)

            elif kind == "progress":
                yield _sse_event("progress", data)

        elapsed_ms = (time.perf_counter() - start) * 1000

        if not answer_text:
            fallback_text = await agent._react._fallback(user_query, history, start, confidence=0.5)
            answer = fallback_text
            yield _sse_event("progress", {"node": "fallback", "message": "未获取到有效回答，切换为降级回答..."})
            yield _sse_event("token", {"text": answer.answer})
        else:
            answer = GeneratedAnswer(
                query=user_query, answer=answer_text, sources=sources,
                confidence=0.85 if sources else 0.7, is_fallback=False,
                tokens_used=count_tokens(answer_text), latency_ms=elapsed_ms,
            )

        yield _sse_event("sources", {"sources": [{"url": s.url, "title": s.title, "snippet": s.snippet} for s in answer.sources]})
        yield _sse_event("done", {"confidence": answer.confidence, "latency_ms": answer.latency_ms, "tokens_used": answer.tokens_used, "is_fallback": answer.is_fallback})

        history.append({"role": "assistant", "content": answer.answer})
        if len(history) > agent_cfg.max_history_turns * 2:
            agent._sessions[session_id] = history[-(agent_cfg.max_history_turns * 2):]
        else:
            agent._sessions[session_id] = history

        if user_id:
            get_user_store().set_history(user_id, session_id, agent._sessions[session_id])
            saved_to_history = True

    except asyncio.CancelledError:
        print(f"[SSE] 客户端主动中断，正在保存半截对话...")
        if not saved_to_history:
            content = answer_text + "\n\n*(用户已停止生成)*" if answer_text else "*(用户已停止生成或切换了会话)*"
            history.append({"role": "assistant", "content": content})
            agent._sessions[session_id] = history
            if user_id:
                get_user_store().set_history(user_id, session_id, history)
                saved_to_history = True
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        yield _sse_event("error", {"message": str(exc), "code": type(exc).__name__})
    finally:
        if not saved_to_history:
            content = answer_text + "\n\n*(异常中断)*" if answer_text else "*(生成异常中断)*"
            history.append({"role": "assistant", "content": content})
            agent._sessions[session_id] = history
            if user_id:
                get_user_store().set_history(user_id, session_id, history)