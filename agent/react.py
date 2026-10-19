"""
智能搜索助手 — ReAct Agent
包含：动态模型配置、长文截断读取、状态栏、防死循环、禁止独白、条件化审核、并行工具调用引导
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from agent.models import GeneratedAnswer, Source
from agent.tools import (
    calculator,
    current_time,
    graph_search,
    history_search,
    knowledge_search,
    read_tool_output,
    reset_tool_counts,
    search,
    set_current_user_id,
)
from config import get_agent_config, get_api_config, resolve_llm_connection, PROVIDER_PRESETS
from tools.registry import generate_fallback_answer
from utils.context import compress_history
from utils.helpers import count_tokens

# ============================================================
# 主 Agent (Proposer) 系统提示
# ============================================================
REACT_SYSTEM_PROMPT = """你是一个实时信息问答助手，请直接回答用户的问题。

## 可用工具与边界
- `search`: 实时联网搜索，用于最新新闻/股价/天气等**时效性互联网信息**
- `read_tool_output`: 读取因为过长被截断的完整搜索结果。当 `search` 的结果末尾提示了 `[系统提示: ...请调用 read_tool_output 工具读取...]`，且当前摘要信息不足以回答问题时，调用此工具读取。
- `knowledge_search`: 本地知识库检索，用于**内部文档/制度/历史决策**等沉淀知识
- `graph_search`: 知识图谱检索，用于需要**多跳推理/精确消歧**的垂直场景
- `history_search`: 检索用户**过往会话**，用于「我上次问过什么」等回忆类问题
- `calculator`: 精确数值计算
- `current_time`: 获取当前时间

## 决策规则
1. **先判断**：回答用户问题需要什么信息？
   - 最新/时效性信息 → `search`
   - 内部文档/沉淀知识 → `knowledge_search`
   - 多跳关系推理/实体消歧 → `graph_search`
   - 回忆过往对话/历史提问 → `history_search`
   - 精确计算 → `calculator`
   - 询问当前时间/日期 → `current_time`
   - 常识/翻译/逻辑推理等 → 直接作答，不调工具
2. **按需多次调用**：一次结果不足可换更精确的查询词再次调用；如果结果被截断，调用 `read_tool_output` 读全文。
3. **★ 并行工具调用（极其重要）**：如果你的问题涉及**多个相互独立的实体或子问题**（例如：对比 A 和 B、查询三个城市的天气、对比三款手机），**请务必在同一轮回复中，一次性输出多个 tool_calls！** 绝对不要一次只搜一个。LangGraph 会自动并发执行它们，极大节省时间。
4. **自主停止**：信息充分后立即停止调用工具，直接给出最终答案。
5. **强制读取**：如果你在 `search` 的结果中看到了 `[系统提示: ...请调用 read_tool_output...]`，**你必须**先调用 `read_tool_output` 读取完整内容后再作答。严禁仅凭截断的摘要就回答复杂对比类、数据列举类问题。
6. **步数预算控制**：你的总执行步数有限。如果一次并行搜索已经覆盖了核心信息，直接总结作答，不要为了追求完美反复搜索。

## 回答规则（极其重要）
1. **禁止输出内心独白**：**绝对不要**在最终回答中向用户解释你刚才做了什么。严禁出现“我需要调用 read_tool_output”、“根据我找到的资料”这类废话。
2. **基于工具结果回答**: 优先使用工具返回的结果。
3. **标注来源**: 每条关键信息末尾标注来源 [1] [2] 等。
4. **直接作答**: 收到用户问题后，直接给出最终结论。

## 输出格式要求
- **开头**: 一句话直接回答核心问题。
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条标注来源。
- **结尾**: 如有重要参考来源，用简短的「📚 参考来源」列出。
- **格式**: 使用 Markdown，保持简洁。"""

# ============================================================
# Reviewer (审核员) 系统提示
# ============================================================
REVIEWER_SYSTEM_PROMPT = """你是一个严苛的事实核查员（Fact Checker）。
你的任务是审核【草稿答案】是否完全基于【检索证据】生成。

## 审核标准
1. **事实一致性**：草稿中的每个关键事实、数字、日期，必须在证据中有明确出处。严禁模型依靠自身训练数据补充细节（幻觉）。
2. **无操作独白**：草稿必须直接给出答案，严禁包含“根据我查到的资料”、“我将调用xx”等内心独白。
3. **不完整警告**：如果用户问了对比类问题，但草稿只回答了一半，应提示补充。

## 输出格式 (严格 JSON)
如果草稿完全合格，输出：
{"status": "PASS"}

如果发现错误或幻觉，输出修改建议：
{"status": "REVISE", "reason": "指出具体哪里缺少证据或编造了事实", "action": "要求主模型如何修改"}
"""

MAX_REVIEW_EVIDENCE_CHARS = 4000

class ReactAgent:
    """ReAct 自主决策 Agent（含动态模型支持与 Proposer-Reviewer 机制）"""

    def __init__(self, verbose: bool = False):
        api_cfg = get_api_config()
        agent_cfg = get_agent_config()

        if verbose:
            agent_cfg.verbose = True

        conn = resolve_llm_connection()
        # 默认的全局模型
        self._llm = ChatOpenAI(
            model=conn["model"],
            base_url=conn["base"],
            api_key=api_cfg.llm_api_key.get_secret_value(),
            temperature=agent_cfg.react_temperature,
            streaming=True,
        )
        
        self._graph = create_react_agent(
            self._llm,
            tools=[
                search,
                read_tool_output,
                knowledge_search,
                graph_search,
                history_search,
                calculator,
                current_time,
            ],
            prompt=REACT_SYSTEM_PROMPT,
        )
        
        self._recursion_limit = agent_cfg.react_recursion_limit
        if self._recursion_limit < 40:
            self._recursion_limit = 40

    def _get_active_graph(
        self, 
        model_override: Optional[str] = None,
        llm_provider: Optional[str] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        """动态获取 Graph：如果前端传了自定义配置，现场创建一个新的"""
        if not (model_override or llm_provider or api_base or api_key):
            return self._graph, self._llm
            
        api_cfg = get_api_config()
        preset = PROVIDER_PRESETS.get((llm_provider or "").lower(), {})
        
        final_model = model_override or preset.get("model", "gpt-4o-mini")
        final_base = api_base or preset.get("api_base", "https://api.openai.com/v1")
        final_key = api_key or api_cfg.llm_api_key.get_secret_value()

        dynamic_llm = ChatOpenAI(
            model=final_model,
            base_url=final_base,
            api_key=final_key,
            temperature=get_agent_config().react_temperature,
            streaming=True,
        )
        
        dynamic_graph = create_react_agent(
            dynamic_llm,
            tools=[
                search,
                read_tool_output,
                knowledge_search,
                graph_search,
                history_search,
                calculator,
                current_time,
            ],
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

    @staticmethod
    def _extract_evidence(messages: list[BaseMessage]) -> str:
        evidence_texts = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
                clean_content = re.sub(r'<agent_status>.*?</agent_status>', '', msg.content, flags=re.DOTALL)
                evidence_texts.append(clean_content.strip())
        
        full_evidence = "\n---\n".join(evidence_texts)
        if len(full_evidence) > MAX_REVIEW_EVIDENCE_CHARS:
            return full_evidence[:MAX_REVIEW_EVIDENCE_CHARS] + "...[证据截断]"
        return full_evidence

    async def _review_and_correct(self, llm, user_query: str, draft_answer: str, evidence: str) -> str:
        if not evidence:
            return draft_answer

        prompt = f"""## 用户问题\n{user_query}\n\n## 检索证据\n{evidence}\n\n## 草稿答案\n{draft_answer}\n\n请审核草稿。"""

        try:
            review_llm = llm.bind(temperature=0.0)
            response = await review_llm.ainvoke([
                SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
                HumanMessage(content=prompt)
            ])
            
            content = response.content
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match: return draft_answer
            
            result = json.loads(json_match.group(0))
            if result.get("status") == "REVISE":
                if get_agent_config().verbose:
                    print(f"[Reviewer 拦截] {result.get('reason')}")
                
                fix_prompt = f"""你之前的回答被驳回了。\n原因：{result.get('reason')}\n修改要求：{result.get('action')}\n\n请**仅基于以下证据**，重新生成回答：\n{evidence}\n直接输出正文。"""
                fixed_response = await llm.ainvoke([
                    SystemMessage(content=REACT_SYSTEM_PROMPT),
                    HumanMessage(content=fix_prompt)
                ])
                return fixed_response.content
            return draft_answer
        except Exception as e:
            return draft_answer

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
    ) -> GeneratedAnswer:
        start = time.perf_counter()
        set_current_user_id(user_id or "")
        reset_tool_counts()
        
        active_graph, active_llm = self._get_active_graph(model_override, llm_provider, api_base, api_key)
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

            # 条件化审核（非流式）
            evidence = self._extract_evidence(messages)
            if evidence.strip():
                final_text = await self._review_and_correct(active_llm, user_query, final_text, evidence)

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
    ):
        set_current_user_id(user_id or "")
        reset_tool_counts()

        active_graph, active_llm = self._get_active_graph(model_override, llm_provider, api_base, api_key)
        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))
        
        final_text = ""
        messages = []
        
        async for event in active_graph.astream_events(
            {"messages": input_messages},
            version="v2",
            config={"recursion_limit": self._recursion_limit},
        ):
            if event.get("event") == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and getattr(chunk, "content", ""):
                    final_text += chunk.content
            
            yield event
            
            if event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
                output = event.get("data", {}).get("output")
                if isinstance(output, dict): messages = output.get("messages", [])
                elif isinstance(output, list): messages = output

        if final_text and messages:
            evidence = self._extract_evidence(messages)
            
            # 条件化审核（流式）
            if evidence.strip():
                yield {"event": "progress", "data": {"node": "reviewer", "message": "正在进行事实与幻觉核查..."}}
                revised_text = await self._review_and_correct(active_llm, user_query, final_text, evidence)
                if revised_text != final_text:
                    yield {"event": "revise", "data": {"text": revised_text}}