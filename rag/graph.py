"""
智能搜索助手 — 知识图谱层（纯 Python 零依赖）

作为结构化索引的「专项索引手段」，与自然语言记忆（rag/__init__.py 的 KnowledgeBase）
协同工作。用于需要**多跳推理**与**精确消歧**的垂直场景（医疗问诊、法律案件分析、
家族关系管理等）。

数据模型：
- 实体（Entity）: {id, name, type, attributes{}, aliases[], doc_refs[]}
  - id 为 sha256(规范化 name + type) 的稳定哈希 → 同名同型实体跨文档自动去重/消歧
- 关系（Relation）: {id, source, target, type, attributes{}, doc_refs[]}

持久化：纯 JSON（data/graph/<name>.json），延续项目「零原生扩展冲突」风格。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT

# ============================================================
# 稳定标识
# ============================================================
def _stable_id(*parts: str) -> str:
    """基于规范化 key 生成稳定哈希 id（实体去重/消歧的基础）"""
    norm = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _normalize_name(name: str) -> str:
    """规范化实体名：去首尾空白 + 小写（英文）"""
    return (name or "").strip()


# ============================================================
# 知识图谱
# ============================================================
class KnowledgeGraph:
    """
    通用领域知识图谱（邻接表 + JSON 持久化）。

    支持：
    - add_entities / add_relations: 稳定 id upsert，合并别名与文档引用
    - resolve_entity: 按 name/alias 精确解析唯一实体（消歧核心）
    - traverse: BFS 多跳遍历，返回「实体 -关系-> 实体」路径
    - query_paths: 解析多个实体后查找连通路径（多跳推理）
    """

    def __init__(self, persist_dir: Optional[Path] = None, name: str = "default"):
        self.name = name
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "graph")
        self._store_path = Path(self.persist_dir) / f"{name}.json"

        self._entities: dict[str, dict] = {}   # id -> entity
        self._relations: dict[str, dict] = {}  # id -> relation
        # 邻接表：实体 id -> 相邻关系 id 集合（加速多跳遍历）
        self._adj: dict[str, set[str]] = {}
        self._load()

    # ── 持久化 ──
    def _load(self) -> None:
        if self._store_path.exists():
            try:
                data = json.loads(self._store_path.read_text(encoding="utf-8"))
                self._entities = data.get("entities", {})
                self._relations = data.get("relations", {})
                self._rebuild_adj()
            except (json.JSONDecodeError, OSError):
                self._entities, self._relations, self._adj = {}, {}, {}

    def _save(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entities": self._entities, "relations": self._relations}
        self._store_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _rebuild_adj(self) -> None:
        self._adj = {}
        for rel_id, rel in self._relations.items():
            for end in (rel.get("source", ""), rel.get("target", "")):
                if end:
                    self._adj.setdefault(end, set()).add(rel_id)

    # ── 写入 ──
    def add_entities(self, entities: list[dict]) -> int:
        """批量写入实体。每个 dict 含 name/type/attributes/aliases。返回新增实体数。"""
        added = 0
        for e in entities:
            name = _normalize_name(e.get("name", ""))
            if not name:
                continue
            etype = e.get("type", "entity")
            eid = _stable_id(name, etype)
            aliases = [a for a in e.get("aliases", []) if a and a != name]
            doc_ref = e.get("doc_ref")

            if eid in self._entities:
                # upsert：合并别名与文档引用（消歧）
                existing = self._entities[eid]
                merged_aliases = list(dict.fromkeys(existing.get("aliases", []) + aliases))
                existing["aliases"] = merged_aliases
                existing["attributes"].update(e.get("attributes", {}) or {})
                if doc_ref and doc_ref not in existing.get("doc_refs", []):
                    existing.setdefault("doc_refs", []).append(doc_ref)
            else:
                self._entities[eid] = {
                    "id": eid,
                    "name": name,
                    "type": etype,
                    "attributes": dict(e.get("attributes", {}) or {}),
                    "aliases": aliases,
                    "doc_refs": [doc_ref] if doc_ref else [],
                }
                added += 1
        if added:
            self._save()
        return added

    def add_relations(self, relations: list[dict]) -> int:
        """批量写入关系。每个 dict 含 source/target/type（source/target 可为实体 name 或 id）。"""
        added = 0
        for r in relations:
            source = self._resolve_ref(r.get("source", ""))
            target = self._resolve_ref(r.get("target", ""))
            rtype = r.get("type", "related_to")
            if not source or not target:
                continue
            rid = _stable_id(source, rtype, target)
            if rid in self._relations:
                continue
            self._relations[rid] = {
                "id": rid,
                "source": source,
                "target": target,
                "type": rtype,
                "attributes": dict(r.get("attributes", {}) or {}),
                "doc_refs": [r.get("doc_ref")] if r.get("doc_ref") else [],
            }
            self._adj.setdefault(source, set()).add(rid)
            self._adj.setdefault(target, set()).add(rid)
            added += 1
        if added:
            self._save()
        return added

    def _resolve_ref(self, ref: str) -> str:
        """关系端点可为实体 id 或 name；返回实体 id（找不到则原样返回空）。"""
        if not ref:
            return ""
        if ref in self._entities:
            return ref
        # 按 name 精确匹配
        hit = self.resolve_entity(ref)
        return hit["id"] if hit else ""

    # ── 检索 / 消歧 ──
    def resolve_entity(self, name: str, etype: Optional[str] = None) -> Optional[dict]:
        """按 name / alias 精确解析唯一实体。返回实体 dict 或 None。"""
        name = _normalize_name(name)
        if not name:
            return None
        name_l = name.lower()
        for e in self._entities.values():
            if etype and e.get("type") != etype:
                continue
            if e.get("name", "").lower() == name_l:
                return e
            if any(a.lower() == name_l for a in e.get("aliases", [])):
                return e
        return None

    def get_entity(self, entity_id: str) -> Optional[dict]:
        return self._entities.get(entity_id)

    def traverse(self, entity_id: str, max_hops: int = 2, direction: str = "both") -> list[dict]:
        """
        BFS 多跳遍历。返回「路径」列表，每条路径形如：
        {"path": [(实体 dict) -关系type-> (实体 dict), ...], "hops": n}
        """
        entity_id = self._resolve_ref(entity_id)
        if entity_id not in self._entities:
            return []

        visited: set[tuple[str, int]] = {(entity_id, 0)}
        frontier: list[tuple[str, int, list[dict]]] = [(entity_id, 0, [])]
        paths: list[dict] = []

        while frontier:
            current, hops, path = frontier.pop(0)
            if hops >= max_hops:
                continue
            for rel_id in self._adj.get(current, ()):
                rel = self._relations.get(rel_id)
                if rel is None:
                    continue
                # 依据 direction 决定可遍历方向
                neighbors: list[str] = []
                if direction in ("both", "outbound") and rel["source"] == current:
                    neighbors.append(rel["target"])
                if direction in ("both", "inbound") and rel["target"] == current:
                    neighbors.append(rel["source"])
                for nxt in neighbors:
                    state = (nxt, hops + 1)
                    if state in visited:
                        continue
                    visited.add(state)
                    step = {
                        "source": self._entities.get(rel["source"]),
                        "relation": rel["type"],
                        "target": self._entities.get(rel["target"]),
                    }
                    new_path = path + [step]
                    paths.append({"path": new_path, "hops": hops + 1})
                    frontier.append((nxt, hops + 1, new_path))
        return paths

    def query_paths(self, terms: list[str], max_hops: int = 2, etype: Optional[str] = None) -> list[dict]:
        """
        解析多个实体后查找连通路径（多跳推理）。

        例：家族关系「张三的父亲是谁」→ terms=["张三","父亲"] 会先 resolve 到实体，
        再返回从「张三」出发、途经「父亲」关系的路径。
        """
        resolved: dict[str, dict] = {}
        for t in terms:
            e = self.resolve_entity(t, etype=etype)
            if e:
                resolved[e["id"]] = e
        if not resolved:
            return []

        # 从每个解析到的实体出发做 BFS，收集不重复路径
        all_paths: list[dict] = []
        seen_path_keys: set[str] = set()
        for eid in resolved:
            for p in self.traverse(eid, max_hops=max_hops):
                key = json.dumps(
                    [(s.get("source", {}).get("id"), s.get("relation"), s.get("target", {}).get("id"))
                     for s in p["path"]],
                    ensure_ascii=False,
                )
                if key not in seen_path_keys:
                    seen_path_keys.add(key)
                    all_paths.append(p)
        return all_paths

    # ── 元信息 ──
    def format_paths(self, paths: list[dict], max_paths: int = 10) -> str:
        """
        将 traverse/query_paths 返回的路径列表渲染为 LLM 可读文本。

        例：「张三 -[father_of]-> 李四 -[father_of]-> 王五」
        """
        lines: list[str] = []
        for p in paths[:max_paths]:
            hops: list[str] = []
            for step in p.get("path", []):
                src = step.get("source") or {}
                rel = step.get("relation", "")
                tgt = step.get("target") or {}
                hops.append(f"{src.get('name', '?')} -[{rel}]-> {tgt.get('name', '?')}")
            lines.append(" -> ".join(hops) if hops else "")
        return "\n".join(line for line in lines if line)

    def count_entities(self) -> int:
        return len(self._entities)

    def count_relations(self) -> int:
        return len(self._relations)


# ============================================================
# 单例
# ============================================================
_kg: Optional[KnowledgeGraph] = None


def get_knowledge_graph() -> KnowledgeGraph:
    global _kg
    if _kg is None:
        _kg = KnowledgeGraph()
    return _kg
