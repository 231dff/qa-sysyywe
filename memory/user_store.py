"""
智能搜索助手 — 用户存储（持久化）

解决「金鱼记忆」的第一块地基：把会话从进程内 dict 迁移到按 user_id 隔离的
JSON 持久化，使会话在进程/容器重启后仍可恢复，且归属到具体用户。

数据布局：
    data/sessions/<safe_user_id>.json
        {
          "user_id": "u_xxx",
          "created_at": 1234567.8,
          "sessions": { "<session_id>": [ {role,content}, ... ], ... }
        }

安全：user_id 作为文件名前先做规范化，杜绝路径穿越。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT


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


class UserStore:
    """
    按 user_id 隔离的会话持久化存储（线程安全）。

    - 每个用户一个 JSON 文件，加载时全量读入内存，写入时落盘
    - 会话历史沿用 {role, content} 结构，与 SearchAgent._sessions 完全兼容
    """

    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "sessions")
        self._users: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._load_all()

    # ── 持久化 ──
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

    # ── 用户 ──
    def get_or_create(self, user_id: str) -> dict:
        with self._lock:
            if user_id not in self._users:
                self._users[user_id] = {"created_at": time.time(), "sessions": {}}
                self._save(user_id)
            return self._users[user_id]

    def exists(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._users

    # ── 会话 ──
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
        """删除该用户所有会话（不删用户记录本身）。"""
        with self._lock:
            user = self._users.get(user_id)
            if user:
                user["sessions"] = {}
                self._save(user_id)

    # ── 历史检索（新增） ──
    def search_sessions(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        snippet_max_len: int = 500,
    ) -> list[dict]:
        """
        按关键词检索用户历史会话，返回按相关性排序的 Top-K 消息片段。

        策略：关键词重叠打分（零依赖、离线确定）。
        中文按标点切分，英文按空格切分；重叠越多排名越高。
        """
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return []
            sessions: dict[str, list[dict]] = dict(user.get("sessions", {}))

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


# 全局单例
_user_store: Optional[UserStore] = None


def get_user_store() -> UserStore:
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store