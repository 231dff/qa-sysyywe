"""
反馈存储 — 基于 Redis 的 👍/👎 记录。

功能:
- 写入反馈（按 message_id 唯一，重复提交覆盖）
- 按 session_id 查询
- 按时间倒序取最近 N 条（Day 6 Bad Case 回流）
- 导出为 jsonl（Day 6 用）

Redis 数据结构:
- search_agent:feedback:{message_id}  -> JSON string（单条反馈）
- search_agent:feedback:session:{sid} -> set（message_id 列表）
- search_agent:feedback:recent        -> list（recent 最近 N 条）

无 Redis 时降级为进程内内存，单副本能跑，多副本各自独立。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

try:
    import redis as _redis_lib
except ImportError:
    _redis_lib = None


@dataclass
class FeedbackEntry:
    feedback_id: str
    session_id: str
    message_id: str
    feedback: str  # "up" | "down"
    comment: str = ""
    query: str = ""
    answer_preview: str = ""
    trace_id: str = ""
    user_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class FeedbackStore:
    """Redis 优先，无 Redis 时降级内存。"""

    RECENT_MAX = 1000

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._memory: dict[str, FeedbackEntry] = {}
        self._memory_sessions: dict[str, set[str]] = {}
        self._memory_recent: list[str] = []
        if self._redis is None and _redis_lib is not None:
            host = os.getenv("REDIS_HOST", "localhost")
            port = int(os.getenv("REDIS_PORT", "6379"))
            db = int(os.getenv("REDIS_DB", "0"))
            try:
                client = _redis_lib.Redis(
                    host=host, port=port, db=db,
                    decode_responses=True, socket_timeout=3,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None

    def add(
        self,
        *,
        session_id: str,
        message_id: str,
        feedback: str,
        comment: str = "",
        query: str = "",
        answer_preview: str = "",
        trace_id: str = "",
        user_id: str = "",
    ) -> FeedbackEntry:
        entry = FeedbackEntry(
            feedback_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            message_id=message_id,
            feedback=feedback,
            comment=comment,
            query=query,
            answer_preview=answer_preview,
            trace_id=trace_id,
            user_id=user_id,
        )

        if self._redis:
            try:
                self._redis.set(
                    f"search_agent:feedback:{message_id}",
                    json.dumps(entry.to_dict(), ensure_ascii=False),
                )
                self._redis.sadd(
                    f"search_agent:feedback:session:{session_id}", message_id,
                )
                self._redis.lpush("search_agent:feedback:recent", message_id)
                self._redis.ltrim("search_agent:feedback:recent", 0, self.RECENT_MAX - 1)
                return entry
            except Exception as e:
                print(f"[feedback_store] Redis 写入失败，降级内存: {e}")

        self._memory[message_id] = entry
        self._memory_sessions.setdefault(session_id, set()).add(message_id)
        self._memory_recent.insert(0, message_id)
        self._memory_recent = self._memory_recent[: self.RECENT_MAX]
        return entry

    def get(self, message_id: str) -> Optional[FeedbackEntry]:
        if self._redis:
            try:
                raw = self._redis.get(f"search_agent:feedback:{message_id}")
                if raw:
                    return FeedbackEntry(**json.loads(raw))
            except Exception:
                pass
        return self._memory.get(message_id)

    def list_by_session(self, session_id: str) -> list[FeedbackEntry]:
        items: list[FeedbackEntry] = []
        if self._redis:
            try:
                mids = self._redis.smembers(
                    f"search_agent:feedback:session:{session_id}"
                ) or set()
                for mid in mids:
                    e = self.get(mid)
                    if e:
                        items.append(e)
            except Exception:
                pass
        else:
            for mid in self._memory_sessions.get(session_id, set()):
                e = self._memory.get(mid)
                if e:
                    items.append(e)
        items.sort(key=lambda e: e.ts)
        return items

    def list_recent(self, limit: int = 100) -> list[FeedbackEntry]:
        limit = max(1, min(limit, self.RECENT_MAX))
        mids: list[str] = []
        if self._redis:
            try:
                mids = self._redis.lrange(
                    "search_agent:feedback:recent", 0, limit - 1
                ) or []
            except Exception:
                pass
        if not mids:
            mids = self._memory_recent[:limit]
        items: list[FeedbackEntry] = []
        for mid in mids:
            e = self.get(mid)
            if e:
                items.append(e)
        return items

    def list_bad_cases(self, limit: int = 50) -> list[FeedbackEntry]:
        """Day 6 用：只取 👎。"""
        all_items = self.list_recent(limit=self.RECENT_MAX)
        return [e for e in all_items if e.feedback == "down"][:limit]

    def export_jsonl(self, path: str, only_bad: bool = False) -> int:
        items = (
            self.list_bad_cases(limit=self.RECENT_MAX)
            if only_bad
            else self.list_recent(limit=self.RECENT_MAX)
        )
        with open(path, "w", encoding="utf-8") as f:
            for e in items:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        return len(items)


_feedback_store: Optional[FeedbackStore] = None


def get_feedback_store() -> FeedbackStore:
    global _feedback_store
    if _feedback_store is None:
        _feedback_store = FeedbackStore()
    return _feedback_store