"""
语义缓存 (Semantic Cache)
用于极速命中高频相似问题，降低 LLM 推理和搜索延迟。
"""
from __future__ import annotations

import json
import math
import time
from typing import Optional, Tuple
import redis

from config import get_agent_config

# 提取向量计算的工具（直接复用你 rag 模块里的哈希特征，或者自己写个简单的）
import hashlib

def _hash_features(text: str, dim: int = 256) -> list[float]:
    """确定性的字符 n-gram 哈希特征（L2 归一化）"""
    vec = [0.0] * dim
    text = text.lower()
    for gram_len in (1, 2):
        for i in range(len(text) - gram_len + 1):
            gram = text[i:i + gram_len]
            h = int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16)
            vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]

def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

class SemanticCache:
    def __init__(self, ttl_seconds: int = 86400): # 默认缓存 1 天
        cfg = get_agent_config()
        self._client = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self._prefix = "semantic_cache:"
        self._ttl = ttl_seconds

    def get(self, query: str, threshold: float = 0.95) -> Optional[str]:
        """查找语义相似度大于 threshold 的缓存"""
        try:
            # 为了性能，只取最近的一部分缓存计算（生产环境应该用 Redis Search 或向量库）
            # 这里用最轻量的方式：遍历所有 key (适合小规模演示)
            keys = self._client.keys(f"{self._prefix}*")
            if not keys:
                return None
            
            target_vec = _hash_features(query)
            
            for key in keys:
                data_str = self._client.get(key)
                if not data_str:
                    continue
                data = json.loads(data_str)
                sim = _cosine(target_vec, data["vector"])
                
                if sim >= threshold:
                    print(f"[SemanticCache] 命中！相似度: {sim:.4f}, 原问题: {data['query']}")
                    # 刷新 TTL
                    self._client.expire(key, self._ttl)
                    return data["answer"]
            return None
        except Exception as e:
            print(f"[SemanticCache Error] {e}")
            return None

    def set(self, query: str, answer: str) -> None:
        """写入缓存"""
        try:
            vec = _hash_features(query)
            key = f"{self._prefix}{hashlib.md5(query.encode()).hexdigest()}"
            payload = {
                "query": query,
                "answer": answer,
                "vector": vec,
                "timestamp": time.time()
            }
            self._client.setex(key, self._ttl, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            print(f"[SemanticCache Set Error] {e}")

# 单例
_semantic_cache: Optional[SemanticCache] = None

def get_semantic_cache() -> SemanticCache:
    global _semantic_cache
    if _semantic_cache is None:
        _semantic_cache = SemanticCache()
    return _semantic_cache