"""
智能搜索助手 — 用户存储（持久化）

★ DeepSeek 式仅追加事件日志：
- 每 session 一个 JSONL 文件：data/sessions/{safe_user_id}/{safe_session_id}.jsonl
- 每行一条消息 {"role","content","ts"}
- 不预加载到内存，get_history 直接读文件
- set_history 重写单个 session 文件（写放大从「整个 user」降到「当前 session」）
- 旧 JSON 文件懒迁移：首次 get_history 时若 JSONL 不存在，尝试从旧 JSON 迁移
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT, get_agent_config

_TOKEN_SPLIT_RE = re.compile(r"[\s,，。.！!？?；;：:、\-_/\\()（）\[\]【】\"'“”‘’]+")


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    parts = _TOKEN_SPLIT_RE.split(text.lower())
    return {p for p in parts if len(p) >= 2}


def _safe_user_id(user_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


def _safe_session_id(session_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in session_id)
    return safe[:64] or "anon"


class FileUserStore:
    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "sessions")
        self._lock = threading.RLock()
        self._migrated: set[tuple[str, str]] = set()

    # ── 路径 ──
    def _user_dir(self, user_id: str) -> Path:
        return Path(self.persist_dir) / _safe_user_id(user_id)

    def _session_path(self, user_id: str, session_id: str) -> Path:
        return self._user_dir(user_id) / f"{_safe_session_id(session_id)}.jsonl"

    # ── 读 ──
    def get_history(self, user_id: str, session_id: str) -> list[dict[str, str]]:
        path = self._session_path(user_id, session_id)
        if not path.exists():
            self._try_migrate_legacy(user_id, session_id)
            if not path.exists():
                return []

        history: list[dict[str, str]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    history.append({
                        "role": msg.get("role", ""),
                        "content": msg.get("content", ""),
                    })
        except OSError:
            return []
        return history

    # ── 写 ──
    def set_history(self, user_id: str, session_id: str, history: list[dict[str, str]]) -> None:
        with self._lock:
            path = self._session_path(user_id, session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for msg in history:
                    f.write(json.dumps({
                        "role": msg.get("role", ""),
                        "content": msg.get("content", ""),
                        "ts": msg.get("ts", now),
                    }, ensure_ascii=False) + "\n")
            tmp.replace(path)

    def delete_session(self, user_id: str, session_id: str) -> None:
        with self._lock:
            path = self._session_path(user_id, session_id)
            if path.exists():
                path.unlink()

    def list_sessions(self, user_id: str) -> list[str]:
        d = self._user_dir(user_id)
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.jsonl")]

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            d = self._user_dir(user_id)
            if d.exists():
                shutil.rmtree(d)

    # ── 跨 session 搜索 ──
    def search_sessions(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        snippet_max_len: int = 500,
    ) -> list[dict]:
        d = self._user_dir(user_id)
        if not d.exists():
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        hits: list[tuple[int, str, dict]] = []
        for path in d.glob("*.jsonl"):
            sid = path.stem
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = msg.get("content", "")
                        if not content:
                            continue
                        content_tokens = _tokenize(content)
                        overlap = len(query_tokens & content_tokens)
                        if overlap == 0:
                            continue
                        hits.append((overlap, sid, msg))
            except OSError:
                continue

        hits.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "session_id": sid,
                "role": msg.get("role", ""),
                "content": msg.get("content", "")[:snippet_max_len],
            }
            for _, sid, msg in hits[:top_k]
        ]

    # ── 旧 JSON 懒迁移 ──
    def _try_migrate_legacy(self, user_id: str, session_id: str) -> None:
        key = (_safe_user_id(user_id), _safe_session_id(session_id))
        if key in self._migrated:
            return
        self._migrated.add(key)

        legacy = Path(self.persist_dir) / f"{_safe_user_id(user_id)}.json"
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        sessions = data.get("sessions", {})
        if session_id not in sessions:
            return
        history = sessions[session_id] or []
        if history:
            self.set_history(user_id, session_id, history)


# ============================================================
# Redis 后端（不变）
# ============================================================
class RedisUserStore:
    def __init__(self):
        import redis
        cfg = get_agent_config()
        self._client = redis.Redis(
            host=cfg.redis_host, port=cfg.redis_port, db=cfg.redis_db,
            password=cfg.redis_password or None, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        self._client.ping()
        self._prefix = "user_sessions:"

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}{_safe_user_id(user_id)}"

    def get_or_create(self, user_id: str) -> dict:
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
            self._key(user_id), session_id,
            json.dumps(list(history), ensure_ascii=False),
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        self._client.hdel(self._key(user_id), session_id)

    def list_sessions(self, user_id: str) -> list[str]:
        return self._client.hkeys(self._key(user_id))

    def clear_user(self, user_id: str) -> None:
        self._client.delete(self._key(user_id))

    def search_sessions(
        self, user_id: str, query: str, top_k: int = 5, snippet_max_len: int = 500
    ) -> list[dict]:
        raw_map = self._client.hgetall(self._key(user_id))
        if not raw_map:
            return []
        sessions = {}
        for sid, raw_json in raw_map.items():
            try:
                sessions[sid] = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
        store = FileUserStore.__new__(FileUserStore)
        return store._do_search(sessions, query, top_k, snippet_max_len) if hasattr(store, "_do_search") else _redis_do_search(sessions, query, top_k, snippet_max_len)


def _redis_do_search(sessions, query, top_k, snippet_max_len):
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    hits = []
    for sid, msgs in sessions.items():
        for msg in msgs:
            content = msg.get("content", "")
            if not content:
                continue
            overlap = len(query_tokens & _tokenize(content))
            if overlap == 0:
                continue
            hits.append((overlap, sid, msg))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [
        {"session_id": sid, "role": m.get("role", ""), "content": m.get("content", "")[:snippet_max_len]}
        for _, sid, m in hits[:top_k]
    ]


_user_store: Optional[object] = None


def get_user_store():
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