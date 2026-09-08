"""
智能搜索助手 — ReAct Agent

基于 LangGraph `create_react_agent` 的自主决策 Agent：
模型拿到工具描述（JSON Schema）后，自主决定「是否调用 search、调用几次、
何时停止」，循环由 `tools_condition` 自动路由驱动。

本模块是「单轮、无状态」执行器：`run()` 只执行一轮问答并返回 GeneratedAnswer。
会话历史由上层 SearchAgent 负责维护与裁剪（保持原有语义）。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from agent.models import GeneratedAnswer, Source
from agent.tools import calculator, current_time, graph_search, knowledge_search, search
from config import get_agent_config, get_api_config, resolve_llm_connection
from tools.registry import generate_fallback_answer
from utils.context import compress_history
from utils.helpers import count_tokens

# ============================================================
# ReAct 系统提示
# ============================================================
REACT_SYSTEM_PROMPT = """你是一个实时信息问答助手。你可以使用提供的工具获取信息。

## 可用工具与边界
- `search`: 实时联网搜索，用于最新新闻/股价/天气等**时效性互联网信息**
- `knowledge_search`: 本地知识库检索，用于**内部文档/制度/历史决策**等沉淀知识
- `graph_search`: 知识图谱检索，用于需要**多跳推理/精确消歧**的垂直场景（医疗、法律、家族/组织关系等）
- `calculator`: 精确数值计算
- `current_time`: 获取当前时间

## 决策规则
1. **先判断**：回答用户问题需要什么信息？
   - 最新/时效性信息 → `search`
   - 内部文档/沉淀知识 → `knowledge_search`
   - 多跳关系推理/实体消歧 → `graph_search`
   - 精确计算 → `calculator`；需要当前时间 → `current_time`
   - 常识/翻译/逻辑推理等 → 直接作答，不调工具
2. **按需多次调用**：一次结果不足可换更精确的查询词再次调用
3. **自主停止**：信息充分后立即停止调用工具，直接给出最终答案

## 回答规则
1. **基于工具结果回答**: 优先使用搜索结果，而非训练数据
2. **标注来源**: 每条关键信息末尾标注来源 [1] [2] 等
3. **诚实**: 结果不足时明确说明，不要编造
4. **逐条呈现**: 用简短的要点列表，每条 1-2 句话
5. **时效提示**: 涉及实时信息时注明时间

## 输出格式要求
- **开头**: 一句话直接回答核心问题
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条标注来源
- **结尾**: 如有重要参考来源，用简短的「📚 参考来源」列出
- **禁止**: 不要用大段文字、不要多级标题、不要引用块
- **格式**: 使用 Markdown，保持简洁——多用列表，少用段落"""


# ============================================================
# ReactAgent
# ============================================================
class ReactAgent:
    """ReAct 自主决策 Agent（单轮无状态执行器）"""

    def __init__(self, verbose: bool = False):
        api_cfg = get_api_config()
        agent_cfg = get_agent_config()

        if verbose:
            agent_cfg.verbose = True

        conn = resolve_llm_connection()
        self._llm = ChatOpenAI(
            model=conn["model"],
            base_url=conn["base"],
            api_key=api_cfg.llm_api_key.get_secret_value(),
            temperature=agent_cfg.react_temperature,
            streaming=True,
        )
        self._graph = create_react_agent(
            self._llm,
            tools=[search, knowledge_search, graph_search, calculator, current_time],
            prompt=REACT_SYSTEM_PROMPT,
        )
        self._recursion_limit = agent_cfg.react_recursion_limit

    # ── 内部：历史 dict → BaseMessage（分层压缩，前缀稳定） ──
    @staticmethod
    def _to_messages(
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
    ) -> list[BaseMessage]:
        """
        将 {role, content} 历史转换为 LangChain 消息列表，并对远期历史做分层压缩。
        - role 支持 system：置于消息最前（记忆上下文注入点）
        - 当传入 memory_context 时，作为 system 消息前缀注入（仅在 history 前）
        """
        if not history:
            # 仅有记忆上下文、无历史时，也要产出 system 消息
            messages: list[BaseMessage] = []
            if memory_context:
                messages.append(SystemMessage(content=memory_context))
            return messages
        # 分层压缩：控制跨轮 token 与 KV 占用
        compressed = compress_history(history)
        messages: list[BaseMessage] = []
        # 记忆上下文作为前缀 system 消息，顺序固定以命中 prompt cache
        if memory_context:
            messages.append(SystemMessage(content=memory_context))
        for msg in compressed:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    # ── 内部：从结果消息中提取最终答案与来源 ──
    @staticmethod
    def _extract_result(messages: list[BaseMessage]) -> tuple[str, list[Source]]:
        """
        从 LangGraph 结果消息中提取：
        - 最后一条 AIMessage 的文本作为答案
        - 所有 ToolMessage 中 search 工具返回的 sources 合并去重
        """
        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        sources: list[Source] = []
        seen_urls: set[str] = set()
        for msg in messages:
            # ToolMessage.content 是 search 工具返回的 JSON 字符串
            if not isinstance(msg, ToolMessage):
                continue
            content = msg.content
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
            for item in payload.get("sources", []):
                url = item.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append(Source(
                        url=url,
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                    ))

        return final_text, sources

    async def run(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
    ) -> GeneratedAnswer:
        """
        执行一轮 ReAct 问答。

        Args:
            user_query: 用户问题
            history: 对话历史 [{role, content}, ...]（可选）
            memory_context: 记忆上下文文本（语义+情景记忆，可选，注入为 system 前缀）

        Returns:
            GeneratedAnswer
        """
        start = time.perf_counter()

        # 构建输入消息：历史 + 当前用户问题
        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))

        try:
            result = await self._graph.ainvoke(
                {"messages": input_messages},
                config={"recursion_limit": self._recursion_limit},
            )
            messages: list[BaseMessage] = result.get("messages", [])
            final_text, sources = self._extract_result(messages)

            # 模型未产出有效答案 → 降级回答
            if not final_text:
                return await self._fallback(user_query, history, start, confidence=0.5)

            elapsed_ms = (time.perf_counter() - start) * 1000
            confidence = 0.85 if sources else 0.7
            return GeneratedAnswer(
                query=user_query,
                answer=final_text,
                sources=sources,
                confidence=confidence,
                is_fallback=False,
                tokens_used=count_tokens(final_text),
                latency_ms=elapsed_ms,
            )

        except Exception as exc:
            # 图执行异常 → 二次降级（LLM 失败）
            return await self._fallback(
                user_query, history, start, confidence=0.4, error=str(exc)
            )

    async def _fallback(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]],
        start: float,
        confidence: float,
        error: Optional[str] = None,
    ) -> GeneratedAnswer:
        """搜索/LLM 不可用时降级为纯 LLM 知识回答"""
        fallback_text = await generate_fallback_answer(user_query, history)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return GeneratedAnswer(
            query=user_query,
            answer=fallback_text,
            sources=[],
            confidence=confidence,
            is_fallback=True,
            tokens_used=count_tokens(fallback_text),
            latency_ms=elapsed_ms,
        )

    # ── 流式事件迭代器（供 SSE 桥接） ──
    async def astream_events(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
    ):
        """
        以 astream_events (v2) 逐事件产出 ReAct 执行过程。
        供 agent/streaming.py 桥接为 SSE 事件。
        """
        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))
        async for event in self._graph.astream_events(
            {"messages": input_messages},
            version="v2",
            config={"recursion_limit": self._recursion_limit},
        ):
            yield event
