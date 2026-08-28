"""
智能搜索助手 — ReAct Agent 单元测试

测试范围:
- ReactAgent._to_messages / _extract_result（纯函数，直接测试）
- ReactAgent.run（通过注入 FakeGraph 驱动：happy path / 降级 / 异常）
- SearchAgent 会话管理（get_history / clear_session / deduplicator）
- REACT_SYSTEM_PROMPT 常量
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.graph import SearchAgent
from agent.models import GeneratedAnswer, Source
from agent.react import REACT_SYSTEM_PROMPT, ReactAgent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ============================================================
# 测试夹具：伪造 LangGraph 编译图
# ============================================================
class FakeGraph:
    """伪造 create_react_agent 返回的 CompiledStateGraph"""

    def __init__(self, result_messages=None, exc=None):
        self._result_messages = result_messages or []
        self._exc = exc
        self.ainvoke = AsyncMock(return_value={"messages": self._result_messages})
        if exc:
            self.ainvoke = AsyncMock(side_effect=exc)

    async def astream_events(self, *args, **kwargs):
        """供流式测试的假事件流"""
        for _ in []:  # 默认为空
            yield


def make_search_tool_message(summary="搜索结果", sources=None):
    """构造 search 工具返回的 ToolMessage（content 为 JSON 字符串）"""
    import json
    payload = {"summary": summary, "sources": sources or []}
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id="call-1",
        name="search",
    )


# ============================================================
# _to_messages / _extract_result 纯函数测试
# ============================================================
class TestReactAgentHelpers:
    """ReactAgent 纯函数单元测试"""

    def test_to_messages_converts_roles(self):
        history = [
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
        ]
        messages = ReactAgent._to_messages(history)
        assert len(messages) == 2
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[1], AIMessage)
        assert messages[0].content == "问题1"
        assert messages[1].content == "回答1"

    def test_to_messages_empty_history(self):
        assert ReactAgent._to_messages(None) == []
        assert ReactAgent._to_messages([]) == []

    def test_extract_result_finds_final_ai_message(self):
        messages = [
            HumanMessage(content="问题"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {"query": "x"}, "id": "c1"}]),
            make_search_tool_message("结果", [{"url": "https://a.com", "title": "", "snippet": "s"}]),
            AIMessage(content="最终答案 [1]"),
        ]
        text, sources = ReactAgent._extract_result(messages)
        assert text == "最终答案 [1]"
        assert len(sources) == 1
        assert sources[0].url == "https://a.com"

    def test_extract_result_deduplicates_sources(self):
        messages = [
            HumanMessage(content="q"),
            make_search_tool_message("r", [{"url": "https://a.com", "snippet": "s1"}]),
            make_search_tool_message("r", [{"url": "https://a.com", "snippet": "s2"}]),
            AIMessage(content="answer"),
        ]
        _, sources = ReactAgent._extract_result(messages)
        assert len(sources) == 1

    def test_extract_result_no_ai_message(self):
        messages = [HumanMessage(content="q")]
        text, sources = ReactAgent._extract_result(messages)
        assert text == ""
        assert sources == []


# ============================================================
# ReactAgent.run 测试（注入 FakeGraph）
# ============================================================
class TestReactAgentRun:
    """ReactAgent.run 行为测试"""

    def _make_agent(self, messages=None, exc=None):
        agent = ReactAgent()
        agent._graph = FakeGraph(result_messages=messages, exc=exc)
        return agent

    @pytest.mark.asyncio
    async def test_run_happy_path(self):
        """模型先 search 后作答 → 正常答案 + 来源"""
        messages = [
            HumanMessage(content="今天有什么新闻？"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {"query": "今日新闻"}, "id": "c1"}]),
            make_search_tool_message(
                "新闻摘要",
                [{"url": "https://example.com/1", "title": "", "snippet": "内容"}],
            ),
            AIMessage(content="根据搜索结果，今天有重要新闻 [1]"),
        ]
        agent = self._make_agent(messages=messages)
        result = await agent.run("今天有什么新闻？")

        assert isinstance(result, GeneratedAnswer)
        assert result.is_fallback is False
        assert result.confidence == 0.85
        assert len(result.sources) == 1
        assert result.answer == "根据搜索结果，今天有重要新闻 [1]"
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_run_empty_answer_triggers_fallback(self):
        """模型未产出有效文本 → 降级回答"""
        messages = [HumanMessage(content="问题")]
        agent = self._make_agent(messages=messages)

        async def fake_fallback(query, history, start, confidence=0.5, error=None):
            return GeneratedAnswer(
                query=query, answer="⚠️ 降级回答", sources=[],
                confidence=confidence, is_fallback=True,
                tokens_used=5, latency_ms=0.0,
            )

        with patch.object(agent, "_fallback", side_effect=fake_fallback) as mock_fb:
            result = await agent.run("问题")

        assert result.is_fallback is True
        assert result.confidence == 0.5
        mock_fb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_graph_exception_triggers_secondary_fallback(self):
        """图执行异常 → 二次降级（confidence 0.4）"""
        agent = self._make_agent(exc=RuntimeError("boom"))

        async def fake_fallback(query, history, start, confidence=0.5, error=None):
            return GeneratedAnswer(
                query=query, answer="⚠️ 降级", sources=[],
                confidence=confidence, is_fallback=True,
                tokens_used=5, latency_ms=0.0,
            )

        with patch.object(agent, "_fallback", side_effect=fake_fallback) as mock_fb:
            result = await agent.run("问题")

        assert result.is_fallback is True
        assert result.confidence == 0.4
        # 确认传入了 error
        assert mock_fb.await_args.kwargs.get("error") == "boom"


# ============================================================
# SearchAgent 会话管理测试
# ============================================================
class TestSearchAgentClass:
    """SearchAgent 类独立行为（无外部依赖）"""

    def test_initialization(self):
        agent = SearchAgent()
        assert agent._sessions == {}
        assert agent._react is not None

    def test_initialization_verbose(self):
        agent = SearchAgent(verbose=True)
        from config import get_agent_config
        assert get_agent_config().verbose is True

    def test_get_history_empty(self):
        agent = SearchAgent()
        assert agent.get_history("nonexistent") == []

    def test_clear_session_nonexistent(self):
        agent = SearchAgent()
        agent.clear_session("nonexistent")  # 不应抛异常

    def test_clear_session_removes_history(self):
        agent = SearchAgent()
        agent._sessions["test_session"] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        agent.clear_session("test_session")
        assert agent.get_history("test_session") == []

    def test_deduplicator_initialized(self):
        agent = SearchAgent()
        assert agent.deduplicator is not None

    @pytest.mark.asyncio
    async def test_run_updates_history(self):
        """run 后应累积并裁剪历史"""
        agent = SearchAgent()

        async def fake_run(query, history=None):
            return GeneratedAnswer(
                query=query, answer="回答", sources=[],
                confidence=0.85, is_fallback=False,
                tokens_used=2, latency_ms=0.0,
            )

        agent._react.run = fake_run  # 替换为同步假实现
        result = await agent.run("问题", session_id="s1")

        history = agent.get_history("s1")
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "问题"}
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_run_trims_history(self):
        """历史超过 max_history_turns 时应裁剪"""
        from config import get_agent_config
        agent = SearchAgent()
        cfg = get_agent_config()
        original = cfg.max_history_turns
        cfg.max_history_turns = 1

        async def fake_run(query, history=None):
            return GeneratedAnswer(
                query=query, answer="答", sources=[],
                confidence=0.85, is_fallback=False,
                tokens_used=1, latency_ms=0.0,
            )

        try:
            agent._react.run = fake_run
            await agent.run("问题1", session_id="s1")
            await agent.run("问题2", session_id="s1")
            history = agent.get_history("s1")
            assert len(history) == 2  # 1 轮 = 2 条
            assert history[0]["content"] == "问题2"
        finally:
            cfg.max_history_turns = original


# ============================================================
# REACT_SYSTEM_PROMPT 常量测试
# ============================================================
class TestReactSystemPrompt:
    """ReAct 系统提示常量测试"""

    def test_prompt_is_non_empty(self):
        assert len(REACT_SYSTEM_PROMPT) > 0

    def test_prompt_contains_key_instructions(self):
        assert "搜索" in REACT_SYSTEM_PROMPT
        assert "来源" in REACT_SYSTEM_PROMPT
        assert "Markdown" in REACT_SYSTEM_PROMPT

    def test_prompt_guides_tool_decision(self):
        """提示应指导「何时搜索、何时停止」的自主决策"""
        assert "调用" in REACT_SYSTEM_PROMPT
        assert "停止" in REACT_SYSTEM_PROMPT or "直接作答" in REACT_SYSTEM_PROMPT
