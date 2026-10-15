"""
智能搜索助手 — 会话内存
支持两种后端：进程内 dict（默认）/ Redis（多副本共享）
通过环境变量 SESSION_BACKEND 切换：memory | redis

两者均实现 dict 兼容接口，可直接替换 agent._sessions
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from config import get_agent_config


# ============================================================
# dict 兼容 Mixin
# ============================================================
class _DictCompatMixin:
    """
    让 SessionStore 能像 dict 一样被使用：
        s in store
        store[sid]
        store[sid] = history
        del store[sid]
        len(store)
        for k in store
        store.keys()
        store.pop(sid, default)
        store == {}
    底层调用统一的 exists/get/put/delete/list_sessions/active_sessions
    """

    def __contains__(self, session_id: str) -> bool:
        return self.exists(session_id)

    def __getitem__(self, session_id: str):
        return self.get(session_id)

    def __setitem__(self, session_id: str, history) -> None:
        self.put(session_id, history)

    def __delitem__(self, session_id: str) -> None:
        self.delete(session_id)

    def __len__(self) -> int:
        return self.active_sessions

    def __iter__(self):
        return iter(self.list_sessions())

    def __eq__(self, other) -> bool:
        # 测试里 `assert agent._sessions == {}` 需要
        if isinstance(other, dict):
            if len(self) != len(other):
                return False
            return all(k in other for k in self.list_sessions())
        return NotImplemented

    def __hash__(self):
        return id(self)

    def keys(self):
        return self.list_sessions()

    def pop(self, session_id: str, default=None):
        if not self.exists(session_id):
            return default
        history = self.get(session_id)
        self.delete(session_id)
        return history


# ============================================================
# 内存后端（单副本专用）
# ============================================================
class MemorySessionStore(_DictCompatMixin):
    """线程安全的进程内会话存储"""

    def __init__(self, ttl_seconds: Optional[int] = None):
        self._ttl = ttl_seconds or get_agent_config().session_ttl_seconds
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, default=None) -> list[dict[str, str]]:
        """获取会话历史（自动续期）"""
        with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return default if default is not None else []
            entry["last_access"] = time.time()
            return entry["history"]

    def put(self, session_id: str, history: list[dict[str, str]]) -> None:
        with self._lock:
            self._store[session_id] = {
                "history": history,
                "created_at": self._store.get(session_id, {}).get("created_at", time.time()),
                "last_access": time.time(),
            }

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._store

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = []
        with self._lock:
            for sid, entry in self._store.items():
                if now - entry["last_access"] > self._ttl:
                    expired.append(sid)
            for sid in expired:
                del self._store[sid]
        return len(expired)

    @property
    def active_sessions(self) -> int:
        with self._lock:
            return len(self._store)

    def list_sessions(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())


# ============================================================
# Redis 后端（多副本共享）
# ============================================================
class RedisSessionStore(_DictCompatMixin):
    """
    基于 Redis 的会话存储。
    - Key：{prefix}{session_id}
    - Value：JSON {history, created_at, last_access}
    - TTL 自动过期 + 每次 get 自动续期
    """

    def __init__(self, ttl_seconds: Optional[int] = None):
        import redis
        cfg = get_agent_config()
        self._ttl = ttl_seconds or cfg.session_ttl_seconds
        self._prefix = cfg.redis_key_prefix
        self._client = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        # 启动时暴露连接问题
        self._client.ping()

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def get(self, session_id: str, default=None) -> list[dict[str, str]]:
        raw = self._client.get(self._key(session_id))
        if raw is None:
            return default if default is not None else []
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return default if default is not None else []
        entry["last_access"] = time.time()
        # 自动续期
        self._client.setex(
            self._key(session_id),
            self._ttl,
            json.dumps(entry, ensure_ascii=False),
        )
        return entry["history"]

    def put(self, session_id: str, history: list[dict[str, str]]) -> None:
        key = self._key(session_id)
        raw = self._client.get(key)
        created_at = time.time()
        if raw:
            try:
                created_at = json.loads(raw).get("created_at", created_at)
            except json.JSONDecodeError:
                pass
        entry = {
            "history": history,
            "created_at": created_at,
            "last_access": time.time(),
        }
        self._client.setex(key, self._ttl, json.dumps(entry, ensure_ascii=False))

    def delete(self, session_id: str) -> None:
        self._client.delete(self._key(session_id))

    def exists(self, session_id: str) -> bool:
        return self._client.exists(self._key(session_id)) > 0

    def cleanup_expired(self) -> int:
        # Redis TTL 自动清理
        return 0

    @property
    def active_sessions(self) -> int:
        return sum(1 for _ in self._client.scan_iter(match=f"{self._prefix}*"))

    def list_sessions(self) -> list[str]:
        prefix_len = len(self._prefix)
        return [k[prefix_len:] for k in self._client.scan_iter(match=f"{self._prefix}*")]


# ============================================================
# 工厂 + 单例
# ============================================================
_session_store: Optional[object] = None


def get_session_store():
    """根据 SESSION_BACKEND 环境变量返回对应的会话存储实例"""
    global _session_store
    if _session_store is not None:
        return _session_store

    cfg = get_agent_config()
    backend = (cfg.session_backend or "memory").lower()

    if backend == "redis":
        _session_store = RedisSessionStore()
    else:
        _session_store = MemorySessionStore()
    return _session_store


# 向后兼容
SessionStore = MemorySessionStore