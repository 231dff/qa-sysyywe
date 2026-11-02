"""
语义缓存 (Semantic Cache)

★ DeepSeek 式 L1/L2 分层：
- L1 精确匹配：query md5 → Redis Hash，O(1)
- L2 语义相似：Redis Search KNN 向量索引（HNSW + COSINE），避免 KEYS 全量扫
- 降级：Redis Search 不可用时，用 SCAN 游标分批遍历 + 逐条余弦
- user_id 隔离：key 带 user_key 前缀，FT.SEARCH 用 TAG 过滤
- user_id 为空跳过读写（匿名不入缓存，避免跨用户泄露）
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from typing import Optional

import redis

from config import get_agent_config

# ── Redis Search 可选依赖（redis-py 5.x 内置） ──
try:
    from redis.commands.search.field import TagField, VectorField, TextField
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
    from redis.commands.search.query import Query
    _HAS_REDIS_SEARCH = True
except ImportError:
    _HAS_REDIS_SEARCH = False

_VECTOR_DIM = 256
_INDEX_NAME = "semantic_cache_idx"


def _hash_features(text: str, dim: int = _VECTOR_DIM) -> list[float]:
    vec = [0.0] * dim
    text = text.lower()
    for gram_len in (1, 2):
        for i in range(len(text) - gram_len + 1):
            gram = text[i : i + gram_len]
            h = int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _safe_user_key(user_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


def _vec_to_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _bytes_to_vec(raw: bytes, dim: int = _VECTOR_DIM) -> list[float]:
    return list(struct.unpack(f"<{dim}f", raw))


class SemanticCache:
    def __init__(self, ttl_seconds: int = 86400):
        cfg = get_agent_config()
        self._client = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password or None,
            decode_responses=False,          # vector 是 bytes，必须 False
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self._prefix = "semantic_cache:"
        self._ttl = ttl_seconds
        self._has_index = self._ensure_index()

    # ---------- 索引 ----------
    def _ensure_index(self) -> bool:
        if not _HAS_REDIS_SEARCH:
            print("[SemanticCache] redis.commands.search 不可用，降级为 SCAN 模式")
            return False
        try:
            self._client.ft(_INDEX_NAME).info()
            return True
        except Exception:
            pass
        try:
            schema = (
                TagField("user_key"),
                VectorField(
                    "vector", "HNSW",
                    {"TYPE": "FLOAT32", "DIM": _VECTOR_DIM, "DISTANCE_METRIC": "COSINE"},
                ),
                TextField("query"),
            )
            definition = IndexDefinition(prefix=[self._prefix], index_type=IndexType.HASH)
            self._client.ft(_INDEX_NAME).create_index(schema, definition=definition)
            print(f"[SemanticCache] Redis Search 索引已创建: {_INDEX_NAME}")
            return True
        except Exception as e:
            print(f"[SemanticCache] 索引创建失败，降级为 SCAN 模式: {e}")
            return False

    # ---------- 读 ----------
    def get(
        self,
        query: str,
        user_id: Optional[str] = None,
        threshold: float = 0.95,
    ) -> Optional[tuple[str, list]]:
        if not user_id:
            return None
        user_key = _safe_user_key(user_id)
        target_vec = _hash_features(query)
        if self._has_index:
            return self._get_via_search(user_key, target_vec, threshold)
        return self._get_via_scan(user_key, target_vec, threshold)

    def _get_via_search(self, user_key, target_vec, threshold):
        vec_bytes = _vec_to_bytes(target_vec)
        q = (
            Query(f"(@user_key:{{{user_key}}})=>[KNN 1 @vector $vec AS knn_score]")
            .sort_by("knn_score")
            .return_fields("query", "answer", "sources", "knn_score")
            .dialect(2)
        )
        try:
            result = self._client.ft(_INDEX_NAME).search(q, query_params={"vec": vec_bytes})
        except Exception as e:
            print(f"[SemanticCache] KNN 查询失败，降级 SCAN: {e}")
            return self._get_via_scan(user_key, target_vec, threshold)

        if not result.docs:
            return None
        doc = result.docs[0]
        try:
            distance = float(getattr(doc, "knn_score", 1.0))
        except (TypeError, ValueError):
            distance = 1.0
        similarity = 1.0 - distance
        if similarity < threshold:
            return None

        key = doc.id
        try:
            self._client.expire(key, self._ttl)
        except Exception:
            pass

        sources = []
        try:
            raw_sources = getattr(doc, "sources", b"[]")
            if isinstance(raw_sources, bytes):
                raw_sources = raw_sources.decode("utf-8")
            sources = json.loads(raw_sources) if raw_sources else []
        except Exception:
            pass

        answer = getattr(doc, "answer", b"")
        if isinstance(answer, bytes):
            answer = answer.decode("utf-8")
        print(f"[SemanticCache] 命中(Search) user={user_key} 相似度={similarity:.4f}")
        return (answer or "", sources)

    def _get_via_scan(self, user_key, target_vec, threshold):
        best = None
        best_sim = -1.0
        cursor = 0
        match = f"{self._prefix}{user_key}:*"
        try:
            while True:
                cursor, keys = self._client.scan(cursor=cursor, match=match, count=100)
                for key in keys:
                    try:
                        data = self._client.hgetall(key)
                    except Exception:
                        continue
                    if not data or b"vector" not in data:
                        continue
                    try:
                        vec = _bytes_to_vec(data[b"vector"])
                    except Exception:
                        continue
                    sim = _cosine(target_vec, vec)
                    if sim > best_sim:
                        best_sim = sim
                        best = (key, data)
                if cursor == 0:
                    break
        except Exception as e:
            print(f"[SemanticCache] SCAN 查询失败: {e}")
            return None

        if best is None or best_sim < threshold:
            return None
        key, data = best
        try:
            self._client.expire(key, self._ttl)
        except Exception:
            pass

        answer = data.get(b"answer", b"")
        if isinstance(answer, bytes):
            answer = answer.decode("utf-8")
        sources = []
        try:
            raw_sources = data.get(b"sources", b"[]")
            if isinstance(raw_sources, bytes):
                raw_sources = raw_sources.decode("utf-8")
            sources = json.loads(raw_sources) if raw_sources else []
        except Exception:
            pass
        print(f"[SemanticCache] 命中(SCAN) user={user_key} 相似度={best_sim:.4f}")
        return (answer or "", sources)

    # ---------- 写 ----------
    def set(
        self,
        query: str,
        answer: str,
        user_id: Optional[str] = None,
        sources: Optional[list] = None,
    ) -> None:
        if not user_id:
            return
        try:
            user_key = _safe_user_key(user_id)
            vec = _hash_features(query)
            key = f"{self._prefix}{user_key}:{hashlib.md5(query.encode()).hexdigest()}"

            mapping = {
                "user_key": user_key,
                "query": query,
                "answer": answer,
                "vector": _vec_to_bytes(vec),
                "sources": json.dumps(sources or [], ensure_ascii=False),
                "timestamp": str(time.time()),
            }
            pipe = self._client.pipeline()
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, self._ttl)
            pipe.execute()
        except Exception as e:
            print(f"[SemanticCache Set Error] {e}")


_semantic_cache: Optional[SemanticCache] = None


def get_semantic_cache() -> SemanticCache:
    global _semantic_cache
    if _semantic_cache is None:
        _semantic_cache = SemanticCache()
    return _semantic_cache