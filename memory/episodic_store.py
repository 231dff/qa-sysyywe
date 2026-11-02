"""
智能搜索助手 — 情景记忆（EpisodicStore）

★ 修 20：新增公开 list_episodes(user_id) 方法，
   server.py 不再穿透访问私有 _users 字典。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT


def _safe_user_id(user_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


_TOKEN_RE = re.compile(r"[一-鿿]+|[a-z0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 1]


def _jaccard(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    if not inter:
        return 0.0
    weighted = sum(len(w) for w in inter)
    return weighted / (weighted + max(len(sa), len(sb)))


class EpisodicStore:
    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        max_episodes: int = 500,
        half_life_days: float = 30.0,
    ):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "memory" / "episodes")
        self.max_episodes = max_episodes
        self.half_life_days = half_life_days
        self._users: dict[str, dict] = {}
        self._lock = threading.Lock()
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
                    "updated_at": data.get("updated_at", time.time()),
                    "episodes": data.get("episodes", []),
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
            "updated_at": rec.get("updated_at", time.time()),
            "episodes": rec.get("episodes", []),
        }
        self._path(user_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _get_or_create(self, user_id: str) -> dict:
        if user_id not in self._users:
            self._users[user_id] = {
                "created_at": time.time(),
                "updated_at": time.time(),
                "episodes": [],
            }
        return self._users[user_id]

    # ── 写入 ──
    def append(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        ts: Optional[float] = None,
    ) -> int:
        content = (content or "").strip()
        if not content:
            return self.count(user_id)

        with self._lock:
            rec = self._get_or_create(user_id)
            eps = rec["episodes"]

            if eps and eps[-1].get("role") == role and eps[-1].get("content") == content:
                return len(eps)

            eps.append({
                "session_id": session_id or "",
                "role": role,
                "content": content,
                "ts": ts if ts is not None else time.time(),
            })

            if len(eps) > self.max_episodes:
                del eps[: len(eps) - self.max_episodes]

            rec["updated_at"] = time.time()
            self._save(user_id)
            return len(eps)

    # ── 检索 ──
    def recall(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        max_age_days: Optional[int] = None,
    ) -> list[dict]:
        with self._lock:
            rec = self._users.get(user_id)
            eps = list(rec.get("episodes", [])) if rec else []

        if not eps:
            return []

        query_tokens = _tokenize(query)
        now = time.time()

        scored: list[dict] = []
        for e in eps:
            e_tokens = _tokenize(e.get("content", ""))
            overlap = _jaccard(query_tokens, e_tokens)
            if overlap <= 0.0:
                continue

            ts = e.get("ts") or now
            age_days = (now - ts) / 86400.0
            if max_age_days is not None and age_days > max_age_days:
                continue

            decay = 0.5 ** (age_days / max(self.half_life_days, 1e-6))
            score = overlap * decay

            scored.append({**e, "score": round(score, 5)})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def render(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        max_age_days: Optional[int] = None,
    ) -> str:
        hits = self.recall(user_id, query, top_k=top_k, max_age_days=max_age_days)
        if not hits:
            return ""
        lines = []
        for h in hits:
            speaker = "用户" if h.get("role") == "user" else "助手"
            lines.append(f"- {speaker}: {h.get('content', '')}")
        return "\n".join(lines)

    # ── 读取 / 元信息 ──
    def count(self, user_id: str) -> int:
        with self._lock:
            rec = self._users.get(user_id)
            return len(rec.get("episodes", [])) if rec else 0

    def list_episodes(self, user_id: str) -> list[dict]:
        """
        ★ 修 20：公开接口，返回该用户全部情景片段（原始顺序：旧→新）。
        调用方如需「最新在前」，自行 reversed。
        """
        with self._lock:
            rec = self._users.get(user_id)
            if not rec:
                return []
            return list(rec.get("episodes", []))

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            rec = self._users.get(user_id)
            if rec:
                rec["episodes"] = []
                rec["updated_at"] = time.time()
                self._save(user_id)


_episodic_store: Optional[EpisodicStore] = None


def get_episodic_store() -> EpisodicStore:
    global _episodic_store
    if _episodic_store is None:
        _episodic_store = EpisodicStore()
    return _episodic_store