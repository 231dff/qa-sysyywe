"""
智能搜索助手 — ReAct Agent
"""
from __future__ import annotations

import os
os.environ["LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"] = "0"

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

BASE_SYSTEM_PROMPT = """你是一个实时信息问答助手，请直接回答用户的问题。

## ★ 条件化思考规则（最高优先级）
1. **简单问题（直出）**：如果用户只是打招呼（如“你好”）、闲聊、或者查询非常简单的常识，**请绝对不要输出任何思考过程**，直接给出最终回答。
2. **复杂问题（思考）**：如果问题涉及多步推理、对比分析、或者需要调用工具检索长文，**你必须先把推理过程包裹在 `<thought>` 标签内**，然后再在标签外输出最终回答。格式如下：
   <thought>
   用户需要对比两款手机，我需要先调用 search 工具...
   </thought>
   这是最终的对比答案...

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
7. **★ 强制搜索规则**：如果你遇到关于**特定动漫/影视/小说 IP、历史事件、具体人物动态、或者任何你不确定最新进展**的问题，**请务必先调用 `search` 或 `spawn_search_worker` 工具进行联网搜索**，严禁凭记忆闭卷作答！

## 回答规则（极其重要）
1. **禁止输出内心独白**：**绝对不要**在 `<thought>` 标签外输出任何内心独白。严禁出现“我需要调用 xx”、“根据我找到的资料”这类废话。
2. **基于工具结果回答**: 优先使用工具返回的结果。
3. **标注来源**: 每条关键信息末尾标注来源 [1] [2] 等。
4. **直接作答**: 收到用户问题后，直接给出最终结论。
5. **★ 整体置信度自检（切勿滥用标签）**：
   - **不要**为每一句话都打标签。
   - 只有当核心事实与检索到的 `<chunk>` 矛盾，或完全是你自己捏造的数据时，才在**该段落的末尾**加上一个 `[低置信度]`。
   - 如果你是**基于自己的预训练知识（未调用搜索）** 回答的，只需在**整篇回答的最后**统一加上一次 `[低置信度]` 即可。
6. **★ 丰富的 Emoji 表达（核心风格）**：
   - 回答的第一句话开头必须加上 `🚀`。
   - 在列举关键约束、安全提示、风险警告时，适当使用 `🛡️`、`⚠️`。
   - 在总结、趋势预测或重大数据前，使用 `📈`、`💡`。
   - 在代码块或系统级操作前，使用 `💻`。
   - **注意**：Emoji 必须与文字有空格隔开，自然融入，不要满屏都是，保持专业感。
7. **生成式 UI (Artifact)**：如果用户要求**对比数据、展示趋势、画图表**，请**不要**使用 Markdown 表格。你必须输出一段被 `<artifact>` 标签包裹的 ECharts JSON 配置。

## 输出格式要求
- **开头**: 一句话直接回答核心问题。
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条标注来源。如果是数据对比，请在文字下方附带 `<artifact>` 代码块。
- **结尾**: 如有重要参考来源，用简短的「📚 参考来源」列出。
- **格式**: 使用 Markdown，保持简洁。"""


def _get_llm_kwargs(model_name: str, base_url: str, enable_deep_think: bool) -> dict:
    """
    ★ 条件化思考控制引擎 (API 层)
    """
    kwargs = {}
    model_lower = model_name.lower()
    
    # 阿里云 Qwen 系列
    if "qwen" in model_lower or "dashscope" in base_url:
        kwargs["extra_body"] = {"enable_thinking": enable_deep_think}
        
    return kwargs


class ReactAgent:
    def __init__(self, verbose: bool = False):
        api_cfg = get_api_config()
        agent_cfg = get_agent_config()

        if verbose:
            agent_cfg.verbose = True

        conn = resolve_llm_connection()
        
        # 默认实例化开启思考
        llm_extra_kwargs = _get_llm_kwargs(conn["model"], conn["base"], True)
        
        self._llm = ChatOpenAI(
            model=conn["model"],
            base_url=conn["base"],
            api_key=api_cfg.llm_api_key.get_secret_value(),
            temperature=agent_cfg.react_temperature,
            streaming=True,
            max_retries=3,
            **llm_extra_kwargs
        )
        
        all_tools = self._get_base_tools() + _GLOBAL_MCP_TOOLS

        self._graph = create_react_agent(
            self._llm,
            tools=all_tools,
            prompt=BASE_SYSTEM_PROMPT,
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
        mcp_tools: Optional[list] = None,
        enable_deep_think: bool = True,
        enable_search: bool = True,
    ):
        active_mcp = mcp_tools if mcp_tools is not None else _GLOBAL_MCP_TOOLS
        all_tools = self._get_base_tools() + active_mcp

        # 1. 动态过滤工具：如果关闭搜索，直接剥夺联网能力
        if not enable_search:
            all_tools = [t for t in all_tools if t.name not in ["search", "spawn_search_worker", "read_tool_output"]]

        # 2. 动态拼接 Prompt
        active_prompt = BASE_SYSTEM_PROMPT
        if not enable_deep_think:
            active_prompt += "\n\n## ★ 最高优先级指令\n当前用户已关闭深度思考。严禁输出任何 `<thought>` 标签，直接给出最终回答！"
        if not enable_search:
            active_prompt += "\n\n## ★ 最高优先级指令\n当前用户已关闭联网搜索。严禁使用任何检索工具，请完全基于你的内置知识作答，如果不确定请如实告知。"

        # ★ 核心修复：必须重新实例化 LLM 以注入正确的思考参数！
        conn = resolve_llm_connection()
        api_cfg = get_api_config()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        
        final_model = model_override or preset.get("model") or conn["model"]
        final_base = api_base or preset.get("api_base") or conn["base"]
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()

        # 获取动态 API 参数 (包含 enable_thinking)
        llm_extra_kwargs = _get_llm_kwargs(final_model, final_base, enable_deep_think)

        try:
            dynamic_llm = ChatOpenAI(
                model=final_model,
                base_url=final_base,
                api_key=final_key,
                temperature=get_agent_config().react_temperature,
                streaming=True,
                max_retries=3,
                **llm_extra_kwargs
            )
            
            dynamic_graph = create_react_agent(
                dynamic_llm,
                tools=all_tools,
                prompt=active_prompt,
            )
            return dynamic_graph, dynamic_llm
        except Exception as e:
            print(f"[React] 动态实例化模型失败，回退到默认配置: {e}")
            # 如果因为传参失败崩溃，回退到不带思考开关的默认实例
            fallback_graph = create_react_agent(self._llm, tools=all_tools, prompt=active_prompt)
            return fallback_graph, self._llm

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
        enable_deep_think: bool = True,
        enable_search: bool = True,
    ) -> GeneratedAnswer:
        start = time.perf_counter()
        set_current_user_id(user_id or "")
        reset_tool_counts()
        
        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, active_llm = self._get_active_graph(
            model_override, llm_provider, api_base, api_key, dynamic_mcp_tools,
            enable_deep_think=enable_deep_think, enable_search=enable_search
        )
        
        conn = resolve_llm_connection()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        final_base = api_base or preset.get("api_base") or conn["base"]
        final_key = api_key or get_api_config().llm_api_key.get_secret_value()
        final_model = model_override or preset.get("model") or conn["model"]
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
        enable_deep_think: bool = True,
        enable_search: bool = True,
    ):
        set_current_user_id(user_id or "")
        reset_tool_counts()

        dynamic_mcp_tools = await self._load_dynamic_mcp(mcp_config)
        active_graph, active_llm = self._get_active_graph(
            model_override, llm_provider, api_base, api_key, dynamic_mcp_tools,
            enable_deep_think=enable_deep_think, enable_search=enable_search
        )
        
        conn = resolve_llm_connection()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        final_base = api_base or preset.get("api_base") or conn["base"]
        final_key = api_key or get_api_config().llm_api_key.get_secret_value()
        final_model = model_override or preset.get("model") or conn["model"]
        set_llm_context(final_key, final_base, final_model)

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))
        
        async for event in active_graph.astream_events(
            {"messages": input_messages},
            version="v2",
            config={"recursion_limit": self._recursion_limit},
        ):
            yield event