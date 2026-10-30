"""
智能搜索助手 — SSE 流式传输编排器 (支持自动降级)
"""
from __future__ import annotations

import json
import time
import asyncio
import re
from typing import AsyncGenerator, Optional

from langchain_core.messages import ToolMessage, SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from agent.models import GeneratedAnswer, Source
from config import get_agent_config, get_tdai_config, resolve_llm_connection, get_api_config
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
    enable_deep_think: Optional[bool] = True,
    enable_search: Optional[bool] = True,
    fallback_model: Optional[str] = None, 
    fallback_api_base: Optional[str] = None,
    fallback_api_key: Optional[str] = None,
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
    in_reasoning_mode = False
    tag_buffer = ""
    
    # 状态标记
    is_fallback_mode = False

    try:
        yield _sse_event("progress", {"node": "generate", "message": "正在思考如何回答..."})

        if tdai_cfg.tdai_enabled:
            memory_context = await get_tdai_adapter().recall(user_id, session_id, user_query)
        else:
            memory_context = get_memory_manager().recall(user_id, user_query).render() if user_id else ""

        # 启动主模型的流
        stream = agent._react.astream_events(
            user_query,
            history,
            memory_context=memory_context,
            user_id=user_id,
            model_override=model_override,
            llm_provider=llm_provider,
            api_base=api_base,
            api_key=api_key,
            mcp_config=mcp_config,
            enable_deep_think=enable_deep_think,
            enable_search=enable_search,
        )
        aiter = stream.__aiter__()

        first_chunk_received = False

        while True:
            try:
                timeout = 15.0 if first_chunk_received else 8.0
                event = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
            except asyncio.TimeoutError:
                if not first_chunk_received and not is_fallback_mode:
                    # ★ 触发降级！
                    print("[SSE] 主模型响应超时，触发降级策略...")
                    is_fallback_mode = True
                    yield _sse_event("progress", {"node": "fallback", "message": "网络延迟过高，已为您切换极速直出模式..."})
                    
                    try:
                        conn = resolve_llm_connection()
                        f_model = fallback_model or "qwen-turbo"
                        f_base = fallback_api_base or conn["base"]
                        f_key = fallback_api_key or get_api_config().llm_api_key.get_secret_value()
                        
                        fast_llm = ChatOpenAI(model=f_model, base_url=f_base, api_key=f_key, streaming=True)
                        
                        fast_msgs = [SystemMessage(content="你是一个极速助手，请直接、简短地回答用户。不要使用任何工具。")]
                        if memory_context:
                            fast_msgs.append(SystemMessage(content=memory_context))
                        fast_msgs.append(HumanMessage(content=user_query))
                        
                        fallback_stream = fast_llm.astream(fast_msgs)
                        aiter = fallback_stream.__aiter__()
                        continue
                    except Exception as e:
                        print(f"[SSE] 降级模型请求失败: {e}")
                        yield _sse_event("error", {"message": "降级模型请求失败，请检查配置", "code": "FallbackError"})
                        break
                else:
                    yield _sse_event("heartbeat", {"status": "thinking"})
                    continue
            except StopAsyncIteration:
                break

            # 兼容备用流的处理
            if is_fallback_mode:
                text = getattr(event, "content", "") or ""
                if text:
                    first_chunk_received = True
                    answer_text += text
                    yield _sse_event("token", {"text": text})
                continue

            # 正常 Agent 事件流处理
            kind = event.get("event", "")
            data = event.get("data", {})

            if kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                if getattr(chunk, "tool_call_chunks", None): continue
                
                text = getattr(chunk, "content", "") or ""
                reasoning = ""
                if hasattr(chunk, "additional_kwargs"):
                    reasoning = chunk.additional_kwargs.get("reasoning_content", "") or chunk.additional_kwargs.get("reasoning", "")
                if not reasoning and hasattr(chunk, "response_metadata"):
                    reasoning = chunk.response_metadata.get("reasoning_content", "")
                
                if reasoning:
                    yield _sse_event("reasoning", {"text": reasoning})

                if not text:
                    continue
                
                first_chunk_received = True

                # 标签解析逻辑保持不变
                text = tag_buffer + text
                tag_buffer = ""
                while text:
                    if not in_reasoning_mode:
                        if "<thought>" in text:
                            before, after = text.split("<thought>", 1)
                            if before:
                                answer_text += before
                                yield _sse_event("token", {"text": before})
                            in_reasoning_mode = True
                            text = after
                        elif text.endswith("<") or text.endswith("<t") or text.endswith("<th"):
                            tag_buffer = text
                            break
                        else:
                            answer_text += text
                            yield _sse_event("token", {"text": text})
                            break
                    else:
                        if "</thought>" in text:
                            before, after = text.split("</thought>", 1)
                            if before:
                                yield _sse_event("reasoning", {"text": before})
                            in_reasoning_mode = False
                            text = after
                        elif text.endswith("<") or text.endswith("</t") or text.endswith("</th"):
                            tag_buffer = text
                            break
                        else:
                            yield _sse_event("reasoning", {"text": text})
                            break

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

        elapsed_ms = (time.perf_counter() - start) * 1000
        answer = GeneratedAnswer(
            query=user_query, answer=answer_text, sources=sources,
            confidence=0.85 if sources else 0.7, is_fallback=is_fallback_mode,
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

    except Exception as exc:
        import traceback
        traceback.print_exc()
        yield _sse_event("error", {"message": "服务发生内部错误，请稍后重试", "code": type(exc).__name__})
    finally:
        if not saved_to_history:
            content = answer_text + "\n\n*(异常中断)*" if answer_text else "*(生成异常中断)*"
            history.append({"role": "assistant", "content": content})
            agent._sessions[session_id] = history
            if user_id:
                get_user_store().set_history(user_id, session_id, history)