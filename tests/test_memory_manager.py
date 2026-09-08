"""
智能搜索助手 — 记忆编排层（MemoryManager）单元测试

覆盖 user_id 贯穿的完整链路：
- recall 召回语义 + 情景记忆
- remember 写入情景 + 抽取语义事实
- user_id 为空的安全降级
- user_id 隔离
"""
from __future__ import annotations

import tempfile

import pytest

from memory.episodic_store import EpisodicStore
from memory.fact_store import FactStore
from memory.memory_manager import MemoryManager


class TestMemoryManager:
    def _make(self):
        d = tempfile.mkdtemp()
        fs = FactStore(persist_dir=d + "/facts")
        es = EpisodicStore(persist_dir=d + "/episodes")
        return d, MemoryManager(fact_store=fs, episodic_store=es)

    def test_remember_extracts_facts_from_user(self):
        d, mm = self._make()
        mm.remember("u_alice", "s1", "user", "我叫小明，我住在北京")
        facts = mm.facts.get_facts("u_alice")
        keys = {f["key"] for f in facts}
        assert "名字" in keys
        assert "居住城市" in keys
        # 情景记忆也记录了这条 user 发言
        assert mm.episodes.count("u_alice") == 1

    def test_remember_assistant_no_fact_extraction(self):
        d, mm = self._make()
        mm.remember("u_alice", "s1", "assistant", "好的，已为您记录。")
        # assistant 发言不抽取事实，但会记录情景
        assert mm.facts.count("u_alice") == 0
        assert mm.episodes.count("u_alice") == 1

    def test_recall_injects_facts_and_episodes(self):
        d, mm = self._make()
        mm.remember("u_alice", "s1", "user", "我用 Python 做后端")
        ctx = mm.recall("u_alice", "Python 后端怎么优化")
        assert not ctx.is_empty
        assert "长期记忆" in ctx.render()
        assert "Python" in ctx.facts_text or "Python" in ctx.episodes_text

    def test_recall_empty_user_id(self):
        d, mm = self._make()
        ctx = mm.recall(None, "任意问题")
        assert ctx.is_empty
        assert ctx.render() == ""

    def test_remember_empty_user_id_noop(self):
        d, mm = self._make()
        result = mm.remember(None, "s1", "user", "我叫小明")
        assert result == {"episodes": 0, "facts_added": 0}

    def test_user_isolation(self):
        d, mm = self._make()
        mm.remember("u_alice", "s1", "user", "我住在上海")
        assert mm.facts.count("u_bob") == 0
        assert mm.episodes.count("u_bob") == 0
        assert mm.recall("u_bob", "上海").is_empty

    @pytest.mark.asyncio
    async def test_aremember_matches_remember(self):
        d, mm = self._make()
        await mm.aremember("u_alice", "s1", "user", "我今年30岁")
        facts = mm.facts.get_facts("u_alice")
        keys = {f["key"] for f in facts}
        assert "年龄" in keys
        assert mm.episodes.count("u_alice") == 1
