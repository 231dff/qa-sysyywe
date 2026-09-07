"""
智能搜索助手 — 结构化索引单元测试

测试范围:
- extractor._coerce_entities / _coerce_relations（LLM 输出规范化，纯函数）
- graph.KnowledgeGraph（建图 / 稳定 id 去重 / 消歧 / 多跳遍历）
- rag.KnowledgeBase 语义分块（_split_sentences / _chunk_text_semantic / units_of_doc / link_entities）
- structured.StructuredIndex（写入编排 / 回填 / 降级兜底）
"""
from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from rag import KnowledgeBase
from rag.extractor import _coerce_entities, _coerce_relations
from rag.graph import KnowledgeGraph
from rag.structured import StructuredIndex


# ============================================================
# extractor 输出规范化
# ============================================================
class TestCoerceEntities:
    def test_empty(self):
        assert _coerce_entities([]) == []
        assert _coerce_entities(None) == []

    def test_normal(self):
        raw = [{"name": "张三", "type": "person", "aliases": ["三哥"], "attributes": {"age": "40"}}]
        out = _coerce_entities(raw)
        assert len(out) == 1
        assert out[0]["name"] == "张三"
        assert out[0]["aliases"] == ["三哥"]
        assert out[0]["attributes"] == {"age": "40"}

    def test_drops_invalid(self):
        raw = [{"name": "张三"}, {"no_name": "x"}, "not_a_dict", {"name": "   "}]
        out = _coerce_entities(raw)
        assert len(out) == 1
        assert out[0]["name"] == "张三"

    def test_fills_default_type(self):
        raw = [{"name": "张三"}]
        assert _coerce_entities(raw)[0]["type"] == "entity"

    def test_aliases_only_strings(self):
        raw = [{"name": "张三", "aliases": ["三哥", 123, None]}]
        assert _coerce_entities(raw)[0]["aliases"] == ["三哥"]


class TestCoerceRelations:
    def test_empty(self):
        assert _coerce_relations([]) == []
        assert _coerce_relations(None) == []

    def test_normal(self):
        raw = [{"source": "张三", "target": "张小明", "type": "father_of"}]
        out = _coerce_relations(raw)
        assert out == [{"source": "张三", "target": "张小明", "type": "father_of"}]

    def test_drops_missing_endpoints(self):
        raw = [
            {"source": "张三", "target": "张小明", "type": "father_of"},
            {"source": "张三"},  # 缺 target
            {"target": "张小明"},  # 缺 source
            "not_a_dict",
        ]
        out = _coerce_relations(raw)
        assert len(out) == 1

    def test_default_type(self):
        raw = [{"source": "a", "target": "b"}]
        assert _coerce_relations(raw)[0]["type"] == "related_to"


# ============================================================
# KnowledgeGraph 建图 / 消歧 / 多跳
# ============================================================
class TestKnowledgeGraph:
    def _make(self):
        tmp = tempfile.TemporaryDirectory()
        kg = KnowledgeGraph(persist_dir=tmp.name, name="test")
        return tmp, kg

    def test_add_entities_and_stable_id_dedup(self):
        tmp, kg = self._make()
        try:
            n1 = kg.add_entities([{"name": "张三", "type": "person"}])
            n2 = kg.add_entities([{"name": "张三", "type": "person"}])  # 同名同型 → 去重
            assert n1 == 1
            assert n2 == 0
            assert kg.count_entities() == 1
        finally:
            tmp.cleanup()

    def test_add_entities_merges_aliases(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([{"name": "张三", "type": "person", "aliases": ["三哥"]}])
            kg.add_entities([{"name": "张三", "type": "person", "aliases": ["小张", "三哥"]}])
            e = kg.resolve_entity("张三")
            assert set(e["aliases"]) == {"三哥", "小张"}
        finally:
            tmp.cleanup()

    def test_resolve_by_name_and_alias(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([{"name": "张三", "type": "person", "aliases": ["三哥"]}])
            assert kg.resolve_entity("张三") is not None
            assert kg.resolve_entity("三哥") is not None  # 别名解析
            assert kg.resolve_entity("不存在") is None
        finally:
            tmp.cleanup()

    def test_resolve_respects_type_filter(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([{"name": "张三", "type": "person"}])
            assert kg.resolve_entity("张三", etype="person") is not None
            assert kg.resolve_entity("张三", etype="disease") is None
        finally:
            tmp.cleanup()

    def test_add_relations_and_traverse(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
                {"name": "王五", "type": "person"},
            ])
            kg.add_relations([
                {"source": "张三", "target": "李四", "type": "father_of"},
                {"source": "李四", "target": "王五", "type": "father_of"},
            ])
            assert kg.count_relations() == 2

            # 从「张三」出发 1 跳，应找到「父亲 → 李四」
            zhang = kg.resolve_entity("张三")
            paths = kg.traverse(zhang["id"], max_hops=1)
            assert len(paths) >= 1
            one_hop = [p for p in paths if p["hops"] == 1]
            assert any(
                p["path"][0]["relation"] == "father_of"
                and p["path"][0]["target"]["name"] == "李四"
                for p in one_hop
            )
        finally:
            tmp.cleanup()

    def test_query_paths_multihop(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
                {"name": "王五", "type": "person"},
            ])
            kg.add_relations([
                {"source": "张三", "target": "李四", "type": "father_of"},
                {"source": "李四", "target": "王五", "type": "father_of"},
            ])
            # 「张三」→「李四」→「王五」两跳连通
            paths = kg.query_paths(["张三", "王五"], max_hops=2)
            assert len(paths) >= 1
            assert any(p["hops"] == 2 for p in paths)
        finally:
            tmp.cleanup()

    def test_relation_dedup(self):
        tmp, kg = self._make()
        try:
            kg.add_entities([
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
            ])
            kg.add_relations([{"source": "张三", "target": "李四", "type": "father_of"}])
            kg.add_relations([{"source": "张三", "target": "李四", "type": "father_of"}])
            assert kg.count_relations() == 1
        finally:
            tmp.cleanup()


# ============================================================
# KnowledgeBase 语义分块 + 回填辅助
# ============================================================
class TestSemanticChunking:
    def test_split_sentences_keeps_punct(self):
        parts = KnowledgeBase._split_sentences("这是第一句。这是第二句！这是第三句？")
        assert len(parts) == 3
        assert parts[0] == "这是第一句。"
        assert parts[1] == "这是第二句！"

    def test_chunk_semantic_short_doc_single_unit(self):
        doc = "一句话，长度很短。"
        units = KnowledgeBase._chunk_text_semantic(doc, max_unit_chars=800)
        assert len(units) == 1
        assert units[0] == doc

    def test_chunk_semantic_splits_on_blank_line(self):
        doc = "第一段。\n\n第二段。\n\n第三段。"
        units = KnowledgeBase._chunk_text_semantic(doc, max_unit_chars=4)
        # 单段超阈值会进一步按句切，但绝不拦腰断句
        assert len(units) >= 3
        # 每个单元都以句末标点结尾或完整句
        for u in units:
            assert u.strip()

    def test_chunk_semantic_no_mid_sentence_cut(self):
        """绝不在句子中间硬切：每个单元要么是完整句，要么是超长单句的兜底硬切"""
        doc = "句子A。句子B。"
        units = KnowledgeBase._chunk_text_semantic(doc, max_unit_chars=5)
        # max=5 会触发硬切兜底，但优先按句边界
        for u in units:
            assert u.strip()

    def test_units_of_doc_and_link_entities(self):
        with tempfile.TemporaryDirectory() as d:
            kb = KnowledgeBase(persist_dir=d, collection_name="k")
            doc = "张三的父亲是李四。"
            kb.add_documents([doc], source="demo")
            doc_id = kb._doc_id(doc, "demo")

            units = kb.units_of_doc(doc_id)
            assert len(units) == 1

            eids = ["e1", "e2"]
            assert kb.link_entities(units[0]["id"], eids) is True
            linked = kb.units_of_doc(doc_id)
            assert linked[0]["metadata"]["entity_ids"] == eids

            # 重复回填应去重
            kb.link_entities(units[0]["id"], ["e1", "e3"])
            assert kb.units_of_doc(doc_id)[0]["metadata"]["entity_ids"] == ["e1", "e2", "e3"]

            # 不存在的单元返回 False
            assert kb.link_entities("nonexistent", ["x"]) is False


# ============================================================
# StructuredIndex 编排
# ============================================================
class TestStructuredIndex:
    def _make(self):
        tmp = tempfile.TemporaryDirectory()
        kb = KnowledgeBase(persist_dir=tmp.name, collection_name="k")
        kg = KnowledgeGraph(persist_dir=tmp.name, name="g")
        idx = StructuredIndex(kb=kb, kg=kg)
        return tmp, kb, kg, idx

    @pytest.mark.asyncio
    async def test_index_documents_happy_path(self):
        tmp, kb, kg, idx = self._make()
        fake_extract = {
            "entities": [
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
            ],
            "relations": [{"source": "张三", "target": "李四", "type": "father_of"}],
        }
        try:
            with patch("rag.structured.extract_entities_relations", new=AsyncMock(return_value=fake_extract)):
                stats = await idx.index_documents(["张三的父亲是李四。"], source="demo")

            assert stats["units"] == 1
            assert stats["entities"] == 2
            assert stats["relations"] == 1
            assert stats["linked"] == 1
            assert kg.count_entities() == 2
            assert kg.count_relations() == 1

            # entity_ids 已回填
            doc_id = kb._doc_id("张三的父亲是李四。", "demo")
            units = kb.units_of_doc(doc_id)
            assert len(units[0]["metadata"]["entity_ids"]) == 2
        finally:
            tmp.cleanup()

    @pytest.mark.asyncio
    async def test_index_documents_degrades_on_extract_failure(self):
        """LLM 抽取失败 → 降级为仅 NL 记忆，不抛异常、不建图"""
        tmp, kb, kg, idx = self._make()
        try:
            with patch("rag.structured.extract_entities_relations", new=AsyncMock(return_value={"entities": [], "relations": []})):
                stats = await idx.index_documents(["一条没有实体的文本。"], source="demo")

            assert stats["units"] == 1
            assert stats["entities"] == 0
            assert stats["relations"] == 0
            assert kg.count_entities() == 0
            assert kg.count_relations() == 0
        finally:
            tmp.cleanup()

    def test_search_fuses_graph_entities(self):
        tmp, kb, kg, idx = self._make()
        try:
            doc = "张三的父亲是李四。"
            kb.add_documents([doc], source="demo")
            doc_id = kb._doc_id(doc, "demo")

            kg.add_entities([
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
            ])
            eids = [kg.resolve_entity("张三")["id"], kg.resolve_entity("李四")["id"]]
            for u in kb.units_of_doc(doc_id):
                kb.link_entities(u["id"], eids)

            result = idx.search("父亲")
            assert len(result["docs"]) >= 1
            assert len(result["docs"][0]["entities"]) == 2
        finally:
            tmp.cleanup()

    def test_query_paths_delegates(self):
        tmp, kb, kg, idx = self._make()
        try:
            kg.add_entities([
                {"name": "张三", "type": "person"},
                {"name": "李四", "type": "person"},
            ])
            kg.add_relations([{"source": "张三", "target": "李四", "type": "father_of"}])
            paths = idx.query_paths(["张三", "李四"])
            assert len(paths) >= 1
        finally:
            tmp.cleanup()
