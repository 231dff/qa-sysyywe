"""
智能搜索助手 — RAG 知识库

基于《深入理解 AI Agent》第3章：
- 文档分块（Chunking，3.2.1）
- 混合检索（稠密/哈希特征 + 关键词精确匹配，3.2.4）
- 时效治理（3.3.3）：文档带 timestamp 元数据，检索时按新鲜度加权/过滤

技术选型（生产级、零原生扩展冲突）：
- 默认存储：纯 Python JSON 持久化（无二进制扩展，与 langchain 等共存无 segfault 风险）
- 向量化：默认确定性哈希特征（离线可用）；可选 sentence-transformers（需 RAG_USE_TRANSFORMERS=1 且模型已缓存）
- 可选后端：RAG_BACKEND=chroma 时启用 ChromaDB（需自行确认环境无扩展冲突）
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT

# 惰性导入重依赖，避免未安装时影响核心路径
_embedder = None
_embedder_unavailable = False

_DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


# ============================================================
# Embedding 封装（懒加载，离线降级）
# ============================================================
def get_embedder():
    """
    懒加载本地 embedding 模型。

    生产级策略：
    - 默认走确定性哈希特征检索（稳定、零依赖、零冲突）
    - 仅当 RAG_USE_TRANSFORMERS=1 且模型已本地缓存时，才加载 sentence-transformers
    """
    global _embedder, _embedder_unavailable
    if _embedder is not None:
        return _embedder
    if _embedder_unavailable:
        return None

    if os.environ.get("RAG_USE_TRANSFORMERS") != "1":
        _embedder_unavailable = True
        return None

    model_name = os.environ.get("EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)
    try:
        from sentence_transformers import SentenceTransformer
        # local_files_only=True：离线时立即抛错（不重试），触发哈希降级
        _embedder = SentenceTransformer(model_name, local_files_only=True)
    except Exception:
        _embedder_unavailable = True
        _embedder = None
    return _embedder


def _hash_features(text: str, dim: int = 256) -> list[float]:
    """确定性字符 n-gram 哈希特征向量（L2 归一化），语义相近文本有重叠特征。"""
    vec = [0.0] * dim
    text = text.lower()
    for gram_len in (1, 2):
        for i in range(len(text) - gram_len + 1):
            gram = text[i:i + gram_len]
            h = int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化；模型不可用时降级为确定性哈希特征向量（维度 256）。"""
    model = get_embedder()
    if model is not None:
        vectors = model.encode(texts, normalize_embeddings=True)
        return vectors.tolist()
    return [_hash_features(t) for t in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（向量已归一化时即点积）"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ============================================================
# 知识库核心（纯 Python 持久化，默认后端）
# ============================================================
class KnowledgeBase:
    """
    持久化知识库，支持：
    - add_documents: 写入文档（自动分块 + 向量化 + 元数据）
    - search: 混合检索（向量 + 关键词），按新鲜度加权
    - 时效治理：文档 timestamp 元数据，可设置新鲜度衰减 / 过期过滤

    默认纯 Python JSON 持久化；RAG_BACKEND=chroma 时启用 ChromaDB。
    """

    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        collection_name: str = "knowledge",
        in_memory: bool = False,
    ):
        self.in_memory = in_memory
        self.collection_name = collection_name
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "kb")
        self._store_path = Path(self.persist_dir) / f"{collection_name}.json"

        self._backend = os.environ.get("RAG_BACKEND", "json").lower()
        self._docs: list[dict] = []  # [{id, text, embedding, metadata}]

        if self._backend == "chroma":
            self._init_chroma()
        else:
            self._init_json()

    # ── 后端初始化 ──
    def _init_json(self):
        if not self.in_memory:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._docs = self._load() if not self.in_memory else []

    def _init_chroma(self):
        import chromadb
        global _chroma_client
        if self.in_memory:
            self._client = chromadb.Client()
        else:
            Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
            if _chroma_client is None:
                _chroma_client = chromadb.PersistentClient(path=self.persist_dir)
            self._client = _chroma_client
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )

    def _load(self) -> list[dict]:
        if self._store_path.exists():
            try:
                return json.loads(self._store_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save(self):
        if not self.in_memory:
            self._store_path.write_text(
                json.dumps(self._docs, ensure_ascii=False), encoding="utf-8"
            )

    # ── 分块 ──
    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """按字符滑动分块，带重叠以保留上下文（3.2.1）"""
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start = end - overlap
        return chunks

    @staticmethod
    def _doc_id(text: str, salt: str = "") -> str:
        return hashlib.sha256((text + salt).encode("utf-8")).hexdigest()[:32]

    # ── 写入 ──
    def add_documents(
        self,
        documents: list[str],
        metadatas: Optional[list[dict]] = None,
        source: str = "manual",
    ) -> int:
        """批量写入文档。metadatas 可含 timestamp（ISO）用于时效治理。"""
        now_iso = datetime.now(timezone.utc).isoformat()
        entries = []

        for i, doc in enumerate(documents):
            meta = dict(metadatas[i]) if metadatas and i < len(metadatas) else {}
            meta.setdefault("timestamp", now_iso)
            meta.setdefault("source", source)

            for chunk in self._chunk_text(doc):
                entries.append({
                    "id": self._doc_id(chunk, meta.get("timestamp", "")),
                    "text": chunk,
                    "embedding": _embed_texts([chunk])[0],
                    "metadata": meta,
                })

        if not entries:
            return 0

        if self._backend == "chroma":
            self._collection.add(
                ids=[e["id"] for e in entries],
                documents=[e["text"] for e in entries],
                embeddings=[e["embedding"] for e in entries],
                metadatas=[e["metadata"] for e in entries],
            )
        else:
            existing_ids = {d["id"] for d in self._docs}
            new_entries = [e for e in entries if e["id"] not in existing_ids]
            self._docs.extend(new_entries)
            self._save()
        return len(entries)

    # ── 混合检索 ──
    def search(
        self,
        query: str,
        top_k: int = 5,
        freshness_bias: bool = True,
        max_age_days: Optional[int] = None,
    ) -> list[dict]:
        """
        混合检索：向量相似度 + 关键词加分 + 新鲜度衰减。
        Returns: [{"text", "score", "metadata"}, ...]
        """
        if self._backend == "chroma":
            return self._search_chroma(query, top_k, freshness_bias, max_age_days)

        if not self._docs:
            return []

        query_embedding = _embed_texts([query])[0]
        query_terms = {t for t in query.lower().split() if len(t) >= 2}
        now = time.time()

        scored: list[dict] = []
        for d in self._docs:
            meta = d.get("metadata", {}) or {}
            similarity = _cosine(query_embedding, d.get("embedding", []))

            # 关键词精确匹配加分（3.2.3）
            keyword_bonus = 0.0
            doc_lower = d["text"].lower()
            for term in query_terms:
                if term in doc_lower:
                    keyword_bonus += 0.05
            keyword_bonus = min(keyword_bonus, 0.15)

            # 时效治理（3.3.3）
            freshness = 0.0
            ts = meta.get("timestamp", "")
            if ts:
                try:
                    age_days = (now - datetime.fromisoformat(ts).timestamp()) / 86400.0
                    if max_age_days is not None and age_days > max_age_days:
                        continue
                    if freshness_bias:
                        freshness = 0.1 * (0.5 ** (age_days / 30.0))
                except (ValueError, TypeError):
                    pass

            scored.append({
                "text": d["text"],
                "score": round(similarity + keyword_bonus + freshness, 4),
                "metadata": meta,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def _search_chroma(self, query, top_k, freshness_bias, max_age_days) -> list[dict]:
        if self._collection.count() == 0:
            return []
        query_embedding = _embed_texts([query])[0]
        n_results = min(max(top_k * 3, 10), self._collection.count())
        result = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        docs = result["documents"][0] if result["documents"] else []
        metas = result["metadatas"][0] if result["metadatas"] else []
        dists = result["distances"][0] if result["distances"] else []
        now = time.time()
        query_terms = {t for t in query.lower().split() if len(t) >= 2}
        scored = []
        for i, doc in enumerate(docs):
            meta = metas[i] or {}
            similarity = 1.0 - float(dists[i])
            keyword_bonus = 0.0
            doc_lower = doc.lower()
            for term in query_terms:
                if term in doc_lower:
                    keyword_bonus += 0.05
            keyword_bonus = min(keyword_bonus, 0.15)
            freshness = 0.0
            ts = meta.get("timestamp", "")
            if ts:
                try:
                    age_days = (now - datetime.fromisoformat(ts).timestamp()) / 86400.0
                    if max_age_days is not None and age_days > max_age_days:
                        continue
                    if freshness_bias:
                        freshness = 0.1 * (0.5 ** (age_days / 30.0))
                except (ValueError, TypeError):
                    pass
            scored.append({"text": doc, "score": round(similarity + keyword_bonus + freshness, 4), "metadata": meta})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ── 便捷：从文件导入 ──
    def add_from_file(self, path: str, source: Optional[str] = None) -> int:
        p = Path(path)
        if not p.exists():
            return 0
        content = p.read_text(encoding="utf-8", errors="ignore")
        meta = {"source": source or p.stem}
        return self.add_documents([content], metadatas=[meta])

    def count(self) -> int:
        if self._backend == "chroma":
            return self._collection.count()
        return len(self._docs)


# ============================================================
# 单例
# ============================================================
_kb: Optional[KnowledgeBase] = None


def get_knowledge_base() -> KnowledgeBase:
    """获取知识库单例"""
    global _kb
    if _kb is None:
        _kb = KnowledgeBase()
    return _kb
