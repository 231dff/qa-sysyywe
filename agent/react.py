"""
智能搜索助手 — ReAct Agent (DeepSeek 级极速架构)
"""
from __future__ import annotations

import os
os.environ["LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"] = "0"

import json
import time
import hashlib
from collections import OrderedDict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langgraph.errors import GraphRecursionError
from langchain.agents import create_agent

from agent.models import GeneratedAnswer, Source
from agent.tools import (
    calculator, current_time, graph_search, history_search,
    knowledge_search, read_tool_output, reset_tool_counts, search,
    set_current_user_id, spawn_search_worker, set_llm_context, fetch_webpage_content
)
from config import get_agent_config, get_api_config, resolve_llm_connection
from tools.registry import generate_fallback_answer
from utils.context import compress_history
from utils.helpers import count_tokens
from utils.llm_factory import build_llm

_GLOBAL_MCP_TOOLS = []
_MCP_TOOLS_CACHE = {}

BASE_SYSTEM_PROMPT = """你是一个专业的实时联网问答助手。你的核心目标是：准确、实时地回答用户，绝不瞎编。

## ★ 极速交互原则
1. **必须先输出过渡语！** 当你决定调用任何工具时，**必须先输出一句简短的过渡语**（如：“好的，我马上为您检索最新信息...”）。绝不允许没有任何文字直接发起工具调用！
2. **允许换词多次搜索**：如果一次 `search` 搜索后信息不足或结果不相关，**你可以更换更精确的查询词再次搜索（最多 3 次）**。但是，一旦收集到足够信息，必须立刻停止搜索并作答。

## ★ 抗幻觉铁律（最高优先级，违反将受严惩）
1. **强制搜索（绝对命令）**：只要涉及**新闻、股价、指数、汇率、政策、最新发布**等任何具有时效性的信息，你**必须且只能**先调用 `search` 或 `current_time` 工具。**绝对禁止**在未调用工具的情况下，直接凭内部记忆给出数字或结论！
2. **搜索结果是唯一事实来源**：你的回答必须严格基于工具返回的内容。如果工具返回的结果没有明确提及具体数据，**你必须直接回答“未在最新搜索结果中找到确切信息”**，绝不允许用你训练数据里的旧知识猜测。
3. **识别荒谬前提（陷阱测试）**：如果用户的问题包含明显违背常识的错误前提（例如“马斯克把推特搬到火星了吗”），你**绝不能顺从**，必须直接指出其违背常识，或者基于搜索事实予以纠正。
4. **相对时间必须校准**：如果用户问“昨天”、“今天”、“本周”，你必须**先调用 `current_time`** 确认当前日期，然后再去搜索，并在回答中明确说明“截至X月X日”。

## ★ 结构化思考规则
只有非常复杂的逻辑对比问题，才允许在 `<thought>` 中输出简短的推演（不超过 100 字）。
简单事实查询直接输出过渡语并调用工具，严禁废话。

## 可用工具
- `search`: 实时联网搜索，返回网页摘要。
- `calculator`: 数值计算。
- `current_time`: 当前时间。

## 回答规则
1. **禁止内心独白**：严禁出现“根据我找到的资料”这类废话。
2. **直接作答**：直接给出结论。
3. **Emoji 风格**：开头加 `🚀`，列举约束用 `🛡️`、`⚠️`，趋势用 `📈`。
4. **Markdown 格式**：分点列出，简洁明了。
"""

class ReactAgent:
    _GRAPH_CACHE_MAX = 32

    def __init__(self, verbose: bool = False):
        api_cfg = get_api_config()
        agent_cfg = get_agent_config()

        if verbose:
            agent_cfg.verbose = True

        conn = resolve_llm_connection()

        self._llm = build_llm(
            model=conn["model"],
            base_url=conn["base"],
            api_key=api_cfg.llm_api_key.get_secret_value(),
            temperature=agent_cfg.react_temperature,
        )

        all_tools = self._get_base_tools() + _GLOBAL_MCP_TOOLS
        self._graph = create_agent(
            model=self._llm,
            tools=all_tools,
            system_prompt=BASE_SYSTEM_PROMPT,
        )

        self._recursion_limit = max(agent_cfg.react_recursion_limit, 40)
        self._graph_cache: OrderedDict[tuple, tuple] = OrderedDict()

    def _get_base_tools(self):
        return [
            search, spawn_search_worker, calculator, current_time,
            read_tool_output, fetch_webpage_content,
            knowledge_search, graph_search, history_search,
        ]

    async def _load_dynamic_mcp(self, mcp_config: dict):
        if not mcp_config:
            return _GLOBAL_MCP_TOOLS
        config_key = hashlib.md5(json.dumps(mcp_config, sort_keys=True).encode()).hexdigest()
        if config_key in _MCP_TOOLS_CACHE:
            return _MCP_TOOLS_CACHE[config_key]
        try:
            from langchain.mcp import MCPAdapter
            print(f"[MCP] 动态加载前端配置: {mcp_config}")
            async with MCPAdapter(mcp_config) as adapter:
                tools = await adapter.get_tools()
            _MCP_TOOLS_CACHE[config_key] = tools
            return tools
        except Exception as e:
            print(f"[MCP] 动态加载失败: {e}")
            return _GLOBAL_MCP_TOOLS

    @staticmethod
    def _graph_cache_key(model, base, api_key, deep_think, search_on, mcp_tools) -> tuple:
        mcp_sig = ",".join(sorted(t.name for t in mcp_tools)) if mcp_tools else ""
        return (
            model or "", base or "",
            hashlib.md5((api_key or "").encode()).hexdigest()[:16],
            bool(deep_think), bool(search_on),
            hashlib.md5(mcp_sig.encode()).hexdigest()[:8],
        )

    def _get_active_graph(
        self,
        model_override: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        mcp_tools: Optional[list] = None,
        enable_deep_think: bool = True,
        enable_search: bool = True,
    ):
        active_mcp = mcp_tools if mcp_tools is not None else _GLOBAL_MCP_TOOLS
        conn = resolve_llm_connection()
        api_cfg = get_api_config()

        final_model = model_override or conn["model"]
        final_base = api_base or conn["base"]
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()

        key = self._graph_cache_key(
            final_model, final_base, final_key, enable_deep_think, enable_search, active_mcp,
        )

        if key in self._graph_cache:
            self._graph_cache.move_to_end(key)
            return self._graph_cache[key]

        all_tools = self._get_base_tools() + active_mcp
        if not enable_search:
            all_tools = [t for t in all_tools if t.name not in ["search", "spawn_search_worker"]]

        active_prompt = BASE_SYSTEM_PROMPT
        if not enable_deep_think:
            active_prompt += "\n\n## ★ 当前用户已关闭深度思考。严禁输出 `<thought>` 标签！"
        if not enable_search:
            active_prompt += "\n\n## ★ 当前用户已关闭联网搜索。严禁使用任何检索工具，凭内置知识作答！"

        dynamic_llm = build_llm(
            model=final_model, base_url=final_base, api_key=final_key,
            temperature=get_agent_config().react_temperature,
        )
        dynamic_graph = create_agent(
            model=dynamic_llm, tools=all_tools, system_prompt=active_prompt,
        )

        self._graph_cache[key] = (dynamic_graph, dynamic_llm)
        if len(self._graph_cache) > self._GRAPH_CACHE_MAX:
            self._graph_cache.popitem(last=False)

        return dynamic_graph, dynamic_llm

    @staticmethod
    def _to_messages(history=None, memory_context=None) -> list[BaseMessage]:
        messages: list[BaseMessage] = []
        if memory_context:
            messages.append(SystemMessage(content=memory_context))
        if history:
            compressed = compress_history(history)
            for msg in compressed:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user": messages.append(HumanMessage(content=content))
                elif role == "assistant": messages.append(AIMessage(content=content))
                elif role == "system": messages.append(SystemMessage(content=content))

        cfg = get_agent_config()
        try:
            now = datetime.now(ZoneInfo(cfg.timezone))
            time_str = now.strftime("%Y年%m月%d日 %H:%M:%S %A")
            messages.append(SystemMessage(content=f"【当前时间】{time_str}（{cfg.timezone}）"))
        except Exception:
            pass
        return messages

    @staticmethod
    def _extract_result(messages: list[BaseMessage]) -> tuple[str, list[Source]]:
        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        sources: list[Source] = []
        seen_urls: set[str] = set()
        for msg in messages:
            if not isinstance(msg, ToolMessage) or not isinstance(msg.content, str): continue
            try: payload = json.loads(msg.content)
            except: continue
            for item in payload.get("sources", []):
                url = item.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append(Source(url=url, title=item.get("title", ""), snippet=item.get("snippet", "")))
        return final_text, sources

    def _resolve_context(self, api_base, api_key, model_override):
        conn = resolve_llm_connection()
        api_cfg = get_api_config()
        final_base = api_base or conn["base"]
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()
        final_model = model_override or conn["model"]
        return final_key, final_base, final_model

    async def run(
        self, user_query: str, history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None, user_id: Optional[str] = None,
        model_override: Optional[str] = None, api_base: Optional[str] = None,
        api_key: Optional[str] = None, mcp_config: Optional[dict] = None,
        enable_deep_think: bool = True, enable_search: bool = True,
    ) -> GeneratedAnswer:
        start = time.perf_counter()
        set_current_user_id(user_id or "")
        reset_tool_counts()

        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, _ = self._get_active_graph(
            model_override, api_base, api_key, dynamic_mcp_tools,
            enable_deep_think=enable_deep_think, enable_search=enable_search,
        )

        final_key, final_base, final_model = self._resolve_context(api_base, api_key, model_override)
        set_llm_context(final_key, final_base, final_model)

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))

        try:
            result = await active_graph.ainvoke(
                {"messages": input_messages}, config={"recursion_limit": self._recursion_limit},
            )
            messages: list[BaseMessage] = result.get("messages", [])
            final_text, sources = self._extract_result(messages)

            if not final_text:
                return await self._fallback(user_query, history, start, confidence=0.5)

            elapsed_ms = (time.perf_counter() - start) * 1000
            return GeneratedAnswer(
                query=user_query, answer=final_text, sources=sources,
                confidence=0.85 if sources else 0.7, is_fallback=False,
                tokens_used=count_tokens(final_text), latency_ms=elapsed_ms,
            )
        except GraphRecursionError:
            return GeneratedAnswer(
                query=user_query, answer="抱歉，任务超出步数限制。请缩小问题范围。",
                sources=[], confidence=0.3, is_fallback=True,
                tokens_used=0, latency_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return await self._fallback(user_query, history, start, confidence=0.4, error=str(exc))

    async def _fallback(self, user_query, history, start, confidence, error=None):
        fallback_text = await generate_fallback_answer(user_query, history)
        return GeneratedAnswer(
            query=user_query, answer=fallback_text, sources=[], confidence=confidence,
            is_fallback=True, tokens_used=count_tokens(fallback_text),
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def astream_events(
        self, user_query: str, history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None, user_id: Optional[str] = None,
        model_override: Optional[str] = None, api_base: Optional[str] = None,
        api_key: Optional[str] = None, mcp_config: Optional[dict] = None,
        enable_deep_think: bool = True, enable_search: bool = True,
    ):
        set_current_user_id(user_id or "")
        reset_tool_counts()

        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, _ = self._get_active_graph(
            model_override, api_base, api_key, dynamic_mcp_tools,
            enable_deep_think=enable_deep_think, enable_search=enable_search,
        )

        final_key, final_base, final_model = self._resolve_context(api_base, api_key, model_override)
        set_llm_context(final_key, final_base, final_model)

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))

        async for event in active_graph.astream_events(
            {"messages": input_messages}, version="v2", config={"recursion_limit": self._recursion_limit},
        ):
            yield event