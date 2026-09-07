"""
智能搜索助手 — 结构化索引（StructuredIndex）

知识图谱的胶水层：把三个半成品串成一条完整链路

    KnowledgeBase（语义分块 + 向量检索）
        └─▶ extractor（LLM 实体/关系抽取）
              └─▶ KnowledgeGraph（实体消歧 + 多跳推理）
                    └─▶ 回填 entity_ids 到 KnowledgeBase 单元

职责边界（单一职责）：
- 写入：index_documents 负责「分块 → 抽取 → 建图 → 回填」全流程编排
- 查询：search 融合 KB 混合检索 + 图谱实体/多跳路径；query_paths 直连图谱
- 降级：LLM 抽取失败时静默退化为「仅自然语言记忆，无图谱」，绝不阻断写入

与现有代码风格一致：纯 Python、零新增依赖、失败兜底不抛异常。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from rag import get_knowledge_base
from rag.extractor import extract_entities_relations
from rag.graph import get_knowledge_graph


class StructuredIndex:
    """
    结构化索引：编排自然语言记忆与知识图谱的协同写入与查询。

    用法:
        idx = get_structured_index()
        stats = await idx.index_documents(["张三的儿子叫张小明……"], source="demo")
        paths = idx.query_paths(["张三", "张小明"])
    """

    def __init__(self, kb=None, kg=None):
        self.kb = kb or get_knowledge_base()
        self.kg = kg or get_knowledge_graph()

    # ── 写入：分块 → 抽取 → 建图 → 回填 ──
    async def index_documents(
        self,
        documents: list[str],
        metadatas: Optional[list[dict]] = None,
        source: str = "manual",
    ) -> dict:
        """
        写入文档并建立结构化索引。

        流程：
        1. 交由 KnowledgeBase 语义分块 + 向量化（unit 附带 doc_id / entity_ids 空位）
        2. 对每个文档调用 extractor 抽取实体与关系（异步，失败返回空、不阻断）
        3. 实体/关系写入 KnowledgeGraph（实体先于关系，保证关系端点可解析）
        4. 将解析出的实体 id 回填到该文档所有单元的 entity_ids

        Returns:
            {"units": int, "entities": int, "relations": int, "linked": int}
        """
        ts = datetime.now(timezone.utc).isoformat()

        total_units = 0
        total_entities = 0
        total_relations = 0
        total_linked = 0

        for i, doc in enumerate(documents):
            meta = dict(metadatas[i]) if metadatas and i < len(metadatas) else {}
            # 显式传入 timestamp，保证 StructuredIndex 与 KB 用同一时间戳盐
            meta.setdefault("timestamp", ts)
            meta.setdefault("source", source)

            # 1. 分块 + 向量化 + 持久化（unit 自带 doc_id 与空 entity_ids）
            unit_count = self.kb.add_documents([doc], metadatas=[meta], source=source)
            total_units += unit_count

            # 2. LLM 抽取（失败返回空，静默降级）
            extracted = await extract_entities_relations(doc)

            # 3. 建图：先实体后关系（关系端点依赖实体 id）
            doc_id = self.kb._doc_id(doc, source)
            entities = extracted.get("entities", [])
            relations = extracted.get("relations", [])
            for e in entities:
                e["doc_ref"] = doc_id
            for r in relations:
                r["doc_ref"] = doc_id
            self.kg.add_entities(entities)
            self.kg.add_relations(relations)
            total_entities += len(entities)
            total_relations += len(relations)

            # 4. 解析实体 id（含 upsert 后的去重），回填到该文档所有单元
            if entities:
                entity_ids = list(dict.fromkeys(
                    hit["id"]
                    for e in entities
                    if (hit := self.kg.resolve_entity(e.get("name", ""), e.get("type")))
                ))
                for unit in self.kb.units_of_doc(doc_id):
                    if self.kb.link_entities(unit["id"], entity_ids):
                        total_linked += 1

        return {
            "units": total_units,
            "entities": total_entities,
            "relations": total_relations,
            "linked": total_linked,
        }

    # ── 查询 ──
    def resolve(self, name: str, etype: Optional[str] = None) -> Optional[dict]:
        """按名称/别名精确解析唯一实体（消歧入口）"""
        return self.kg.resolve_entity(name, etype=etype)

    def query_paths(
        self,
        terms: list[str],
        max_hops: int = 2,
        etype: Optional[str] = None,
    ) -> list[dict]:
        """多跳推理：解析多个实体后查找连通路径"""
        return self.kg.query_paths(terms, max_hops=max_hops, etype=etype)

    def search(self, query: str, top_k: int = 5, max_hops: int = 2, **kwargs) -> dict:
        """
        融合检索：KB 混合检索结果 + 每条结果关联的知识图谱实体 + 多跳路径。

        Returns:
            {"docs": [{text, score, metadata, entities: [实体 dict]}...],
             "paths": [多跳路径...]}
        """
        docs = self.kb.search(query, top_k=top_k, **kwargs)
        enriched: list[dict] = []
        seen_entity_ids: set[str] = set()
        entity_names: set[str] = set()
        for r in docs:
            entity_ids = (r.get("metadata") or {}).get("entity_ids") or []
            entities = []
            for eid in entity_ids:
                e = self.kg.get_entity(eid)
                if e and eid not in seen_entity_ids:
                    seen_entity_ids.add(eid)
                    entity_names.add(e["name"])
                    entities.append(e)
            enriched.append({**r, "entities": entities})

        # 从「查询词 + 检索命中实体名」出发做多跳，中文查询无空格时也能借实体名回溯出路径
        terms = list(dict.fromkeys([t for t in query.split() if t] + sorted(entity_names)))
        paths = self.kg.query_paths(terms, max_hops=max_hops)
        return {"docs": enriched, "paths": paths}


# ============================================================
# 单例
# ============================================================
_index: Optional[StructuredIndex] = None


def get_structured_index() -> StructuredIndex:
    """获取结构化索引单例"""
    global _index
    if _index is None:
        _index = StructuredIndex()
    return _index
