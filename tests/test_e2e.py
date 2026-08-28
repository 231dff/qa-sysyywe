"""
智能搜索助手 — Agent 端到端测试（ReAct 范式）

测试范围: 通过注入 FakeGraph 驱动 SearchAgent.run()，验证：
- 多轮对话历史累积与裁剪
- 会话管理与清理
- 结果完整性（来源/延迟/token）
- run_stream 流式契约（Streamlit 用）

说明：ReAct 循环的自主决策由 langgraph 内部驱动，这里不假设固定调用顺序，
而是断言语义结果（是否有答案、是否有来源、历史是否正确累积）。
"""
from __future__ import annotations

import json

import pytest

from agent.graph import SearchAgent
from agent.models import GeneratedAnswer, Source
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ============================================================
# 测试夹具
# ============================================================
def make_search_tool_message(summary="搜索结果", sources=None):
    payload = {"summary": summary, "sources": sources or []}
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id="call-1",
        name="search",
    )


def make_happy_messages(query, answer="这是基于搜索结果的回答。", with_sources=True):
    """构造一次完整的 ReAct 轨迹：question → tool_call → tool_result → answer"""
    msgs = [HumanMessage(content=query)]
    if with_sources:
        msgs.append(AIMessage(
            content="",
            tool_calls=[{"name": "search", "args": {"query": "测试"}, "id": "c1"}],
        ))
        msgs.append(make_search_tool_message(
            "结果", [{"url": "https://example.com/1", "title": "", "snippet": "内容"}]
        ))
    msgs.append(AIMessage(content=answer))
    return msgs


class FakeGraph:
    def __init__(self, messages_factory):
        self._messages_factory = messages_factory
        self.ainvoke = None  # 由 patch 注入

    async def astream_events(self, *args, **kwargs):
        for _ in []:
            yield


def install_fake_react(agent, messages_factory):
    """替换 ReactAgent._graph，使 run() 返回预设轨迹"""
    graph = FakeGraph(messages_factory)

    async def fake_ainvoke(inputs, config=None):
        query = inputs["messages"][-1].content
        return {"messages": messages_factory(query)}

    graph.ainvoke = fake_ainvoke
    agent._react._graph = graph
    return agent


# ============================================================
# 端到端: 多轮对话 & 历史
# ============================================================
class TestAgentPipelineE2E:
    """Agent 完整流程端到端测试"""

    @pytest.mark.asyncio
    async def test_full_pipeline_happy_path(self):
        """正常流程: ReAct 搜索后作答, 验证最终答案与来源"""
        agent = install_fake_react(
            SearchAgent(), lambda q: make_happy_messages(q)
        )
        query = "今天有什么重大新闻？"

        result = await agent.run(query)

        assert isinstance(result, GeneratedAnswer)
        assert result.query == query
        assert len(result.answer) > 0
        assert result.is_fallback is False
        assert result.confidence == 0.85
        assert result.latency_ms >= 0
        assert result.tokens_used >= 0

        # 来源
        assert len(result.sources) >= 1
        for src in result.sources:
            assert isinstance(src, Source)
            assert src.url.startswith("https://")

        # 历史更新
        assert len(agent._sessions) == 1
        session_id = list(agent._sessions.keys())[0]
        history = agent.get_history(session_id)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == query
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_search_failure_triggers_fallback(self):
        """模型未产出有效答案 → 降级回答"""
        agent = install_fake_react(
            SearchAgent(), lambda q: [HumanMessage(content=q)]
        )

        async def fake_fallback(query, history, start, confidence=0.5, error=None):
            return GeneratedAnswer(
                query=query, answer="⚠️ 无法实时搜索，基于训练数据回答。",
                sources=[], confidence=confidence, is_fallback=True,
                tokens_used=5, latency_ms=0.0,
            )

        agent._react._fallback = fake_fallback
        result = await agent.run("今天有什么新闻？")

        assert result.is_fallback is True
        assert result.confidence == 0.5
        assert len(result.sources) == 0
        assert len(result.answer) > 0

    @pytest.mark.asyncio
    async def test_answer_generation_failure_triggers_secondary_fallback(self):
        """图执行异常 → 二次降级（confidence 0.4）"""
        class BoomGraph:
            async def ainvoke(self, *a, **k):
                raise RuntimeError("LLM API 500")

            async def astream_events(self, *a, **k):
                for _ in []:
                    yield

        agent = SearchAgent()
        agent._react._graph = BoomGraph()

        async def fake_fallback(query, history, start, confidence=0.5, error=None):
            return GeneratedAnswer(
                query=query, answer="⚠️ 降级答案", sources=[],
                confidence=confidence, is_fallback=True,
                tokens_used=5, latency_ms=0.0,
            )

        agent._react._fallback = fake_fallback
        result = await agent.run("今天有什么新闻？")

        assert result.is_fallback is True
        assert result.confidence == 0.4
        assert len(result.answer) > 0

    @pytest.mark.asyncio
    async def test_multi_turn_conversation_history(self):
        """多轮对话: 历史应正确累积"""
        agent = install_fake_react(
            SearchAgent(), lambda q: make_happy_messages(q, answer=f"回答: {q}")
        )
        session_id = "test-multi-turn"

        result1 = await agent.run("今天有什么新闻？", session_id=session_id)
        history1 = agent.get_history(session_id)
        assert len(history1) == 2

        result2 = await agent.run("详细说说第一条", session_id=session_id)
        history2 = agent.get_history(session_id)
        assert len(history2) == 4

        assert history2[0]["role"] == "user"
        assert history2[0]["content"] == "今天有什么新闻？"
        assert history2[1]["role"] == "assistant"
        assert history2[2]["role"] == "user"
        assert history2[2]["content"] == "详细说说第一条"
        assert history2[3]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_history_trimming(self):
        """历史超过 max_history_turns 时应被裁剪"""
        from config import get_agent_config
        agent = install_fake_react(
            SearchAgent(), lambda q: make_happy_messages(q, answer="答")
        )
        session_id = "test-trim"

        agent_cfg = get_agent_config()
        original = agent_cfg.max_history_turns
        agent_cfg.max_history_turns = 2

        try:
            for i in range(4):
                await agent.run(f"问题 {i+1}", session_id=session_id)

            history = agent.get_history(session_id)
            assert len(history) == 4  # 2 轮 = 4 条
            assert history[0]["content"] == "问题 3"
            assert history[2]["content"] == "问题 4"
        finally:
            agent_cfg.max_history_turns = original


# ============================================================
# Session 管理
# ============================================================
class TestSessionManagement:
    """会话管理测试"""

    @pytest.mark.asyncio
    async def test_auto_session_id_generation(self):
        agent = install_fake_react(SearchAgent(), lambda q: make_happy_messages(q))
        await agent.run("test")
        assert len(agent._sessions) == 1

    @pytest.mark.asyncio
    async def test_clear_session(self):
        agent = install_fake_react(SearchAgent(), lambda q: make_happy_messages(q))
        session_id = "test-clear"

        await agent.run("问题1", session_id=session_id)
        assert len(agent.get_history(session_id)) == 2

        agent.clear_session(session_id)
        assert agent.get_history(session_id) == []


# ============================================================
# 结果验证
# ============================================================
class TestResultValidation:
    """端到端结果完整性验证"""

    @pytest.mark.asyncio
    async def test_answer_contains_sources_when_search_succeeds(self):
        agent = install_fake_react(SearchAgent(), lambda q: make_happy_messages(q))
        result = await agent.run("test query")

        assert len(result.sources) > 0
        urls = [s.url for s in result.sources]
        assert len(urls) == len(set(urls))

    @pytest.mark.asyncio
    async def test_latency_is_recorded(self):
        agent = install_fake_react(SearchAgent(), lambda q: make_happy_messages(q))
        result = await agent.run("test")
        assert result.latency_ms >= 0
        assert result.tokens_used >= 0

    @pytest.mark.asyncio
    async def test_run_stream_yields_status_and_answer(self):
        """run_stream 应依次产出 status 和 answer"""
        agent = install_fake_react(SearchAgent(), lambda q: make_happy_messages(q))

        outputs = []
        async for item in agent.run_stream("test query"):
            outputs.append(item)

        assert len(outputs) >= 2
        assert outputs[0]["type"] == "status"
        assert outputs[-1]["type"] == "answer"
