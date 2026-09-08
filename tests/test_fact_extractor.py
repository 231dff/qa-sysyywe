"""
智能搜索助手 — 语义事实抽取（fact_extractor）单元测试

覆盖规则式抽取的各类长期事实，与可选 LLM 兜底的降级语义。
"""
from __future__ import annotations

import pytest

from memory.fact_extractor import extract_facts, extract_facts_rules


class TestExtractFactsRules:
    def test_extract_name(self):
        facts = extract_facts_rules("我叫小明，很高兴认识你")
        names = {f["key"]: f for f in facts}
        assert "名字" in names
        assert names["名字"]["value"] == "小明"

    def test_extract_city(self):
        facts = extract_facts_rules("我住在北京，已经十年了")
        cities = {f["key"]: f for f in facts}
        assert "居住城市" in cities
        assert "北京" in cities["居住城市"]["value"]

    def test_extract_tech_stack(self):
        facts = extract_facts_rules("我主要用 Python 做后端")
        stacks = {f["key"]: f for f in facts}
        assert "技术栈" in stacks
        assert "Python" in stacks["技术栈"]["value"]

    def test_extract_age(self):
        facts = extract_facts_rules("我今年30岁")
        ages = {f["key"]: f for f in facts}
        assert "年龄" in ages
        assert ages["年龄"]["value"] == "30"

    def test_no_facts_for_transient_query(self):
        """一次性请求不应被抽取为长期事实"""
        facts = extract_facts_rules("帮我查一下今天北京的天气")
        assert facts == []

    def test_empty_input(self):
        assert extract_facts_rules("") == []

    def test_confidence_range(self):
        facts = extract_facts_rules("我叫小李，住在上海")
        for f in facts:
            assert 0.0 <= f["confidence"] <= 1.0


class TestExtractFactsAsync:
    @pytest.mark.asyncio
    async def test_extract_facts_rules_default_no_llm(self):
        """默认 use_llm=False 时仅规则抽取，不触发 LLM 调用"""
        facts = await extract_facts("我住在杭州，用 Java 做后端")
        keys = {f["key"] for f in facts}
        assert "居住城市" in keys
        assert "技术栈" in keys
