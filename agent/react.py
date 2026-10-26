"""
智能搜索助手 — ReAct Agent
"""
from __future__ import annotations

import json
import time
import hashlib
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from agent.models import GeneratedAnswer, Source
from agent.tools import (
    calculator, current_time, graph_search, history_search,
    knowledge_search, read_tool_output, reset_tool_counts, search,
    set_current_user_id, spawn_search_worker, set_llm_context,
)
from config import get_agent_config, get_api_config, resolve_llm_connection, PROVIDER_PRESETS
from tools.registry import generate_fallback_answer
from utils.context import compress_history
from utils.helpers import count_tokens

_GLOBAL_MCP_TOOLS = []
_MCP_TOOLS_CACHE = {}

REACT_SYSTEM_PROMPT = """你是一个实时信息问答助手，请直接回答用户的问题。

## 可用工具与边界
- `spawn_search_worker`: **（最高优先级）** 委派子 Agent 去独立检索并提炼信息。适用于复杂对比、多实体查询、或者需要阅读大量长文的场景。
- `search`: **（核心工具）** 实时联网深度搜索。它会自动抓取网页正文、切片并进行语义重排，直接返回带有 `<context_pool>` 标签的精华上下文。你无需进行额外读取。
- `knowledge_search`: 本地知识库检索，用于内部沉淀知识。
- `graph_search`: 知识图谱检索，用于多跳推理/精确消歧。
- `history_search`: 检索用户过往会话。
- `calculator`: 精确数值计算。
- `current_time`: 获取当前时间。
- **MCP 动态工具**：如果你看到了以 get_ 开头的工具，那是动态接入的 MCP 工具，请直接调用。

## 决策规则
1. **★ 复杂任务委派（隔离上下文）**：
   - 如果问题涉及**对比多个独立实体**（例如：对比 3 款手机），或者需要深入调查某个复杂话题，**请务必并行调用 `spawn_search_worker` 工具！**
   - 千万不要自己去直接调 `search` 去查长文。把每个子任务委派给 `spawn_search_worker`，它们会各自在独立的上下文中完成长文检索，并只把几百字的摘要交还给你。
   - 收到所有摘要后，你再进行最终的汇总和对比。
2. **简单任务直接搜索**：如果只是查一个简单的天气、股价，优先使用 MCP 工具或 `search`。
3. **按需多次调用**：一次结果不足可换更精确的查询词再次调用。
4. **自主停止**：信息充分后立即停止调用工具，直接给出最终答案。
5. **直接利用上下文池作答**：你在 `search` 工具返回的结果中会看到 `<context_pool>` 标签，里面包含了多条精炼的 `<chunk>`。请直接从这些 chunk 中提取事实作答。
6. **步数预算控制**：你的总执行步数有限。如果一次并行搜索已经覆盖了核心信息，直接总结作答，不要为了追求完美反复搜索。

## 回答规则（极其重要）
1. **禁止输出内心独白**：**绝对不要**在最终回答中向用户解释你刚才做了什么。严禁出现“我需要调用 read_tool_output”、“根据我找到的资料”这类废话。
2. **基于工具结果回答**: 优先使用工具返回的结果。
3. **标注来源**: 每条关键信息末尾标注来源 [1] [2] 等。
4. **直接作答**: 收到用户问题后，直接给出最终结论。
5. **★ 内生置信度自检**：对于每一个关键事实陈述，请快速核对它是否直接来自你刚刚看到的 `<chunk>`。**如果某个陈述你无法在上下文中找到直接支持（或者是基于你的常识补充的），请在句末立刻加上 `[低置信度]` 标记。** 如果有明确支持，则不需要加任何标记。
6. **生成式 UI (Artifact)**：如果用户要求**对比数据、展示趋势、画图表**，请**不要**使用 Markdown 表格。你必须输出一段被 `<artifact>` 标签包裹的 ECharts JSON 配置。
   格式如下：
   <artifact>
   {
     "title": { "text": "图表标题" },
     "tooltip": {},
     "legend": { "data": ["系列1", "系列2"] },
     "xAxis": { "type": "category", "data": ["类别A", "类别B"] },
     "yAxis": { "type": "value" },
     "series": [
       { "name": "系列1", "type": "bar", "data": [120, 200] },
       { "name": "系列2", "type": "bar", "data": [150, 300] }
     ]
   }
   </artifact>

## 输出格式要求
- **开头**: 一句话直接回答核心问题。
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条标注来源。如果是数据对比，请在文字下方附带 `<artifact>` 代码块。
- **结尾**: 如有重要参考来源，用简短的「📚 参考来源」列出。
- **格式**: 使用 Markdown，保持简洁。"""

class ReactAgent:
    def __init__(self, verbose: bool = False):
        api_cfg = get_api_config()
        agent_cfg = get_agent_config()

        if verbose:
            agent_cfg.verbose = True

        conn = resolve_llm_connection()
        # ★ 确保 stream_chunk_timeout=300 生效
        self._llm = ChatOpenAI(
            model=conn["model"],
            base_url=conn["base"],
            api_key=api_cfg.llm_api_key.get_secret_value(),
            temperature=agent_cfg.react_temperature,
            streaming=True,
            max_retries=3,
            stream_chunk_timeout=300, 
        )
        
        all_tools = self._get_base_tools() + _GLOBAL_MCP_TOOLS

        self._graph = create_react_agent(
            self._llm,
            tools=all_tools,
            prompt=REACT_SYSTEM_PROMPT,
        )
        
        self._recursion_limit = agent_cfg.react_recursion_limit
        if self._recursion_limit < 40:
            self._recursion_limit = 40

    def _get_base_tools(self):
        return [
            spawn_search_worker,
            search,
            read_tool_output,
            knowledge_search,
            graph_search,
            history_search,
            calculator,
            current_time,
        ]

    async def _load_dynamic_mcp(self, mcp_config: dict):
        if not mcp_config:
            return _GLOBAL_MCP_TOOLS
            
        config_key = hashlib.md5(json.dumps(mcp_config, sort_keys=True).encode()).hexdigest()
        if config_key in _MCP_TOOLS_CACHE:
            return _MCP_TOOLS_CACHE[config_key]
            
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
            print(f"[MCP] 动态加载前端配置: {mcp_config}")
            client = MultiServerMCPClient(mcp_config)
            tools = await client.get_tools()
            _MCP_TOOLS_CACHE[config_key] = tools
            return tools
        except Exception as e:
            print(f"[MCP] 动态加载失败: {e}")
            return _GLOBAL_MCP_TOOLS

    def _get_active_graph(
        self, 
        model_override: Optional[str] = None,
        llm_provider: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        mcp_tools: Optional[list] = None
    ):
        active_mcp = mcp_tools if mcp_tools is not None else _GLOBAL_MCP_TOOLS
        all_tools = self._get_base_tools() + active_mcp

        if not (model_override or llm_provider or api_base or api_key):
            if mcp_tools and len(active_mcp) > len(_GLOBAL_MCP_TOOLS):
                dynamic_graph = create_react_agent(self._llm, tools=all_tools, prompt=REACT_SYSTEM_PROMPT)
                return dynamic_graph, self._llm
            return self._graph, self._llm
            
        api_cfg = get_api_config()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        
        final_model = model_override or preset.get("model", "gpt-4o-mini")
        final_base = api_base or preset.get("api_base", "https://api.openai.com/v1")
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()

        # ★ 同样加上超时参数
        dynamic_llm = ChatOpenAI(
            model=final_model,
            base_url=final_base,
            api_key=final_key,
            temperature=get_agent_config().react_temperature,
            streaming=True,
            max_retries=3,
            stream_chunk_timeout=300,
        )
        
        dynamic_graph = create_react_agent(
            dynamic_llm,
            tools=all_tools,
            prompt=REACT_SYSTEM_PROMPT,
        )
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
            if not isinstance(msg, ToolMessage) or not isinstance(msg.content, str):
                continue
            try:
                payload = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                continue
            for item in payload.get("sources", []):
                url = item.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append(Source(url=url, title=item.get("title", ""), snippet=item.get("snippet", "")))
        return final_text, sources

    async def run(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
        user_id: Optional[str] = None,
        model_override: Optional[str] = None,
        llm_provider: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        mcp_config: Optional[dict] = None,
    ) -> GeneratedAnswer:
        start = time.perf_counter()
        set_current_user_id(user_id or "")
        reset_tool_counts()
        
        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, active_llm = self._get_active_graph(model_override, llm_provider, api_base, api_key, dynamic_mcp_tools)
        
        api_cfg = get_api_config()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        final_base = api_base or preset.get("api_base", "https://api.openai.com/v1")
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()
        final_model = model_override or preset.get("model", "gpt-4o-mini")
        set_llm_context(final_key, final_base, final_model)

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))

        try:
            result = await active_graph.ainvoke(
                {"messages": input_messages},
                config={"recursion_limit": self._recursion_limit},
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
            is_fallback=True, tokens_used=count_tokens(fallback_text), latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def astream_events(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
        user_id: Optional[str] = None,
        model_override: Optional[str] = None,
        llm_provider: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        mcp_config: Optional[dict] = None,
    ):
        set_current_user_id(user_id or "")
        reset_tool_counts()

        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, active_llm = self._get_active_graph(model_override, llm_provider, api_base, api_key, dynamic_mcp_tools)
        
        api_cfg = get_api_config()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        final_base = api_base or preset.get("api_base", "https://api.openai.com/v1")
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()
        final_model = model_override or preset.get("model", "gpt-4o-mini")
        set_llm_context(final_key, final_base, final_model)

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))
        
        async for event in active_graph.astream_events(
            {"messages": input_messages},
            version="v2",
            config={"recursion_limit": self._recursion_limit},
        ):
            yield event