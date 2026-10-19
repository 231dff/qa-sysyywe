"""
智能搜索助手 — 用户存储（持久化）

解决「金鱼记忆」的第一块地基：把会话从进程内 dict 迁移到按 user_id 隔离的
持久化存储，使会话在进程/容器重启后仍可恢复，且归属到具体用户。

支持两种后端：
- file: 本地 JSON 文件持久化
- redis: 云原生 Redis 持久化（支持多副本 Pod 共享）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT, get_agent_config


# ============================================================
# 分词辅助（零依赖，简单切分中英文）
# ============================================================
_TOKEN_SPLIT_RE = re.compile(r"[\s,，。.！!？?；;：:、\-_/\\()（）\[\]【】\"'“”‘’]+")


def _tokenize(text: str) -> set[str]:
    """简单分词：按标点/空白切分，仅保留长度 ≥ 2 的片段。"""
    if not text:
        return set()
    parts = _TOKEN_SPLIT_RE.split(text.lower())
    return {p for p in parts if len(p) >= 2}


def _safe_user_id(user_id: str) -> str:
    """规范化 user_id：仅保留 [a-zA-Z0-9_-]，其余折叠，防路径穿越。"""
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


# ============================================================
# 文件后端
# ============================================================
class FileUserStore:
    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "sessions")
        self._users: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._load_all()

    def _path(self, user_id: str) -> Path:
        return Path(self.persist_dir) / f"{_safe_user_id(user_id)}.json"

    def _load_all(self) -> None:
        d = Path(self.persist_dir)
        if not d.exists():
            return
        for p in d.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                uid = data.get("user_id", p.stem)
                self._users[uid] = {
                    "created_at": data.get("created_at", time.time()),
                    "sessions": data.get("sessions", {}),
                }
            except (json.JSONDecodeError, OSError):
                continue

    def _save(self, user_id: str) -> None:
        d = Path(self.persist_dir)
        d.mkdir(parents=True, exist_ok=True)
        rec = self._users.get(user_id, {})
        payload = {
            "user_id": user_id,
            "created_at": rec.get("created_at", time.time()),
            "sessions": rec.get("sessions", {}),
        }
        self._path(user_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def get_or_create(self, user_id: str) -> dict:
        with self._lock:
            if user_id not in self._users:
                self._users[user_id] = {"created_at": time.time(), "sessions": {}}
                self._save(user_id)
            return self._users[user_id]

    def get_history(self, user_id: str, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return []
            return user.get("sessions", {}).get(session_id, [])

    def set_history(self, user_id: str, session_id: str, history: list[dict[str, str]]) -> None:
        with self._lock:
            self.get_or_create(user_id)["sessions"][session_id] = list(history)
            self._save(user_id)

    def delete_session(self, user_id: str, session_id: str) -> None:
        with self._lock:
            user = self._users.get(user_id)
            if user:
                user.get("sessions", {}).pop(session_id, None)
                self._save(user_id)

    def list_sessions(self, user_id: str) -> list[str]:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return []
            return list(user.get("sessions", {}).keys())

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            user = self._users.get(user_id)
            if user:
                user["sessions"] = {}
                self._save(user_id)

    def search_sessions(self, user_id: str, query: str, top_k: int = 5, snippet_max_len: int = 500) -> list[dict]:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return []
            sessions: dict[str, list[dict]] = dict(user.get("sessions", {}))
        return self._do_search(sessions, query, top_k, snippet_max_len)

    def _do_search(self, sessions, query, top_k, snippet_max_len):
        if not sessions:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        hits: list[tuple[int, str, dict]] = []
        for sid, msgs in sessions.items():
            for msg in msgs:
                content = msg.get("content", "")
                if not content:
                    continue
                content_tokens = _tokenize(content)
                overlap = len(query_tokens & content_tokens)
                if overlap == 0:
                    continue
                hits.append((overlap, sid, msg))

        hits.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "session_id": sid,
                "role": msg.get("role", ""),
                "content": msg.get("content", "")[:snippet_max_len],
            }
            for _, sid, msg in hits[:top_k]
        ]


# ============================================================
# Redis 后端
# ============================================================
class RedisUserStore:
    def __init__(self):
        import redis
        cfg = get_agent_config()
        self._client = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        # 测试连接
        self._client.ping()
        self._prefix = "user_sessions:"

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}{_safe_user_id(user_id)}"

    def get_or_create(self, user_id: str) -> dict:
        # Redis Hash 不需要显式创建
        return {}

    def get_history(self, user_id: str, session_id: str) -> list[dict[str, str]]:
        raw = self._client.hget(self._key(user_id), session_id)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    def set_history(self, user_id: str, session_id: str, history: list[dict[str, str]]) -> None:
        self._client.hset(
            self._key(user_id),
            session_id,
            json.dumps(list(history), ensure_ascii=False)
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        self._client.hdel(self._key(user_id), session_id)

    def list_sessions(self, user_id: str) -> list[str]:
        return self._client.hkeys(self._key(user_id))

    def clear_user(self, user_id: str) -> None:
        self._client.delete(self._key(user_id))

    def search_sessions(self, user_id: str, query: str, top_k: int = 5, snippet_max_len: int = 500) -> list[dict]:
        # 从 Redis 中拉取该用户的所有会话
        raw_map = self._client.hgetall(self._key(user_id))
        if not raw_map:
            return []

        sessions = {}
        for sid, raw_json in raw_map.items():
            try:
                sessions[sid] = json.loads(raw_json)
            except json.JSONDecodeError:
                continue

        # 复用文件版的搜索逻辑
        store = FileUserStore(in_memory=True) if hasattr(FileUserStore, "in_memory") else FileUserStore.__new__(FileUserStore)
        return store._do_search(sessions, query, top_k, snippet_max_len)


# ============================================================
# 工厂 + 单例
# ============================================================
_user_store: Optional[object] = None


def get_user_store():
    """根据环境变量选择后端"""
    global _user_store
    if _user_store is not None:
        return _user_store

    cfg = get_agent_config()
    backend = (cfg.session_backend or "file").lower()

    if backend == "redis":
        _user_store = RedisUserStore()
    else:
        _user_store = FileUserStore()
        
    return _user_store

UserStore = FileUserStore