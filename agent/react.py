"""
智能搜索助手 — ReAct Agent
包含：长文截断读取、状态栏、防死循环、禁止独白、Proposer-Reviewer 双阶段审核机制
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
from config import get_agent_config, get_api_config, resolve_llm_connection
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
- `graph_search`: 知识图谱检索，用于需要**多跳推理/精确消歧**的垂直场景（医疗、法律、家族/组织关系等）
- `history_search`: 检索用户**过往会话**，用于「我上次问过什么」「之前我们讨论过什么」等回忆类问题
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
3. **自主停止**：信息充分后立即停止调用工具，直接给出最终答案。
4. **强制读取**：如果你在 `search` 的结果中看到了 `[系统提示: ...请调用 read_tool_output...]`，**你必须**先调用 `read_tool_output` 读取完整内容后再作答。严禁仅凭截断的摘要就回答复杂对比类、数据列举类问题，否则将被判定为失败。
5. **步数预算控制**：你的总执行步数有限（最多约 40 步）。请高效规划：
   - 如果一次搜索已经覆盖了核心信息，直接总结作答，不要为了追求完美反复搜索。
   - 如果调用 read_tool_output 读取了长文，请立刻基于长文提炼答案。
   - 对于多维度对比，尽量用一次精准的搜索获取主体信息，不要为每个小细节单独拆分搜索，避免耗尽步数。

## 回答规则（极其重要）
1. **禁止输出内心独白**：**绝对不要**在最终回答中向用户解释你刚才做了什么。例如，严禁出现“我需要调用 read_tool_output 读取全文”、“搜索结果被截断，我现在去查”、“根据我找到的资料”这类操作汇报。用户只看结果，不关心你的操作过程。
2. **基于工具结果回答**: 优先使用工具返回的结果，而非训练数据。
3. **标注来源**: 每条关键信息末尾标注来源 [1] [2] 等。
4. **诚实**: 结果不足时明确说明，不要编造。
5. **直接作答**: 收到用户问题后，直接给出最终结论。只有在完全没有查到信息时，才说明“抱歉，未查找到相关内容”。

## 输出格式要求
- **开头**: 一句话直接回答核心问题。
- **中间**: 用无序列表 (- ) 逐条列出关键信息，每条标注来源。
- **结尾**: 如有重要参考来源，用简短的「📚 参考来源」列出。
- **禁止**: 不要用大段文字、不要多级标题、不要引用块、**绝对禁止包含任何操作过程的废话**。
- **格式**: 使用 Markdown，保持简洁——多用列表，少用段落"""

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

# 限制提取证据的最大长度，防止 Reviewer 的 Context 爆炸
MAX_REVIEW_EVIDENCE_CHARS = 4000


class ReactAgent:
    """ReAct 自主决策 Agent（含 Proposer-Reviewer 机制）"""

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
        
        # 如果配置的限制太低，强制兜底为 40
        self._recursion_limit = agent_cfg.react_recursion_limit
        if self._recursion_limit < 40:
            self._recursion_limit = 40

    @staticmethod
    def _to_messages(
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
    ) -> list[BaseMessage]:
        """将 {role, content} 历史转换为 LangChain 消息列表，并对远期历史做分层压缩。"""
        messages: list[BaseMessage] = []

        if memory_context:
            messages.append(SystemMessage(content=memory_context))

        if history:
            compressed = compress_history(history)
            for msg in compressed:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role == "assistant":
                    messages.append(AIMessage(content=content))
                elif role == "system":
                    messages.append(SystemMessage(content=content))

        cfg = get_agent_config()
        try:
            now = datetime.now(ZoneInfo(cfg.timezone))
            time_str = now.strftime("%Y年%m月%d日 %H:%M:%S %A")
            messages.append(SystemMessage(
                content=f"【当前时间】{time_str}（{cfg.timezone}）"
            ))
        except Exception:
            pass

        return messages

    @staticmethod
    def _extract_result(messages: list[BaseMessage]) -> tuple[str, list[Source]]:
        """从 LangGraph 结果消息中提取：最后一条 AIMessage 文本和 ToolMessage sources。"""
        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        sources: list[Source] = []
        seen_urls: set[str] = set()
        for msg in messages:
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

    @staticmethod
    def _extract_evidence(messages: list[BaseMessage]) -> str:
        """从轨迹中提取所有工具返回的检索证据，供 Reviewer 审核。"""
        evidence_texts = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
                # 过滤掉我们注入的状态栏标签，只拿纯内容
                clean_content = re.sub(r'<agent_status>.*?</agent_status>', '', msg.content, flags=re.DOTALL)
                evidence_texts.append(clean_content.strip())
        
        # 拼接并截断，防止过长
        full_evidence = "\n---\n".join(evidence_texts)
        if len(full_evidence) > MAX_REVIEW_EVIDENCE_CHARS:
            return full_evidence[:MAX_REVIEW_EVIDENCE_CHARS] + "...[证据截断]"
        return full_evidence

    async def _review_and_correct(
        self, 
        user_query: str, 
        draft_answer: str, 
        evidence: str
    ) -> str:
        """
        调用独立视角的 Reviewer，核对草稿与证据的一致性。
        如果有幻觉或违规，让 Reviewer 直接重写，返回修复后的答案。
        """
        if not evidence:
            return draft_answer

        prompt = f"""## 用户问题
{user_query}

## 检索证据 (不可篡改的事实依据)
{evidence}

## 草稿答案 (待审核)
{draft_answer}

请审核草稿。如果发现幻觉、事实错误或包含了操作废话，输出 REVISE 及修改建议；否则输出 PASS。"""

        try:
            # 使用低温度，确保审核结果稳定
            review_llm = self._llm.bind(temperature=0.0)
            response = await review_llm.ainvoke([
                SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
                HumanMessage(content=prompt)
            ])
            
            # 尝试解析 JSON
            content = response.content
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                return draft_answer
            
            result = json.loads(json_match.group(0))
            
            if result.get("status") == "REVISE":
                if get_agent_config().verbose:
                    print(f"[Reviewer 拦截] 发现幻觉: {result.get('reason')}")
                
                # 触发自我纠错：把修改建议发给主模型，要求重写
                fix_prompt = f"""你之前的回答被事实核查员驳回了。
原因：{result.get('reason')}
修改要求：{result.get('action')}

请**仅基于以下证据**，重新生成最终回答：
{evidence}

不要输出任何解释，直接输出修改后的正文。"""
                fixed_response = await self._llm.ainvoke([
                    SystemMessage(content=REACT_SYSTEM_PROMPT),
                    HumanMessage(content=fix_prompt)
                ])
                return fixed_response.content

            return draft_answer
            
        except Exception as e:
            if get_agent_config().verbose:
                print(f"[Reviewer 异常] 审核失败: {e}")
            return draft_answer  # 出错时回退到原答案，不影响主流程

    async def run(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> GeneratedAnswer:
        """执行一轮 ReAct 问答（同步，无流式）。"""
        start = time.perf_counter()
        set_current_user_id(user_id or "")
        reset_tool_counts()
        
        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))

        try:
            result = await self._graph.ainvoke(
                {"messages": input_messages},
                config={"recursion_limit": self._recursion_limit},
            )
            messages: list[BaseMessage] = result.get("messages", [])
            final_text, sources = self._extract_result(messages)

            if not final_text:
                return await self._fallback(user_query, history, start, confidence=0.5)

            # ★ 调用 Reviewer 进行事实核查
            evidence = self._extract_evidence(messages)
            final_text = await self._review_and_correct(user_query, final_text, evidence)

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

        except GraphRecursionError:
            if get_agent_config().verbose:
                print(f"[ReAct 保护] 达到最大步数限制 ({self._recursion_limit})，触发强制收尾。")
            return GeneratedAnswer(
                query=user_query,
                answer="抱歉，这个问题涉及的搜索与对比维度过多，我在搜集信息时达到了系统的分析步数上限。请您尝试缩小问题的范围，或者分步骤向我提问。",
                sources=[],
                confidence=0.3,
                is_fallback=True,
                tokens_used=0,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        except Exception as exc:
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

    async def astream_events(
        self,
        user_query: str,
        history: Optional[list[dict[str, str]]] = None,
        memory_context: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        """以 astream_events (v2) 逐事件产出 ReAct 执行过程（带尾部静默审核）。"""
        set_current_user_id(user_id or "")
        reset_tool_counts()

        input_messages = self._to_messages(history, memory_context=memory_context)
        input_messages.append(HumanMessage(content=user_query))
        
        # 1. 流式产出
        final_text = ""
        messages = []
        
        async for event in self._graph.astream_events(
            {"messages": input_messages},
            version="v2",
            config={"recursion_limit": self._recursion_limit},
        ):
            # 拦截文本 token，用于拼装草稿
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if getattr(chunk, "content", ""):
                    final_text += chunk.content
            
            yield event
            
            # 记录最终状态的消息列表（用于提取证据）
            if event["event"] == "on_chain_end" and event["name"] == "LangGraph":
                messages = event["data"]["output"].get("messages", [])

        # 2. 流式结束，开始静默审核并纠错
        if final_text and messages:
            evidence = self._extract_evidence(messages)
            
            # 向前端发送一个状态更新，提示正在进行事实核查
            yield {
                "event": "progress", 
                "data": json.dumps({"node": "reviewer", "message": "正在进行事实与幻觉核查..."}, ensure_ascii=False)
            }
            
            revised_text = await self._review_and_correct(user_query, final_text, evidence)
            
            # 3. 如果 Reviewer 修改了答案，向前端发一个覆盖事件
            if revised_text != final_text:
                yield {
                    "event": "revise", 
                    "data": json.dumps({"text": revised_text}, ensure_ascii=False)
                }