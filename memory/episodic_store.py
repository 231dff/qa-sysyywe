"""
智能搜索助手 — 情景记忆（EpisodicStore）

解决「金鱼记忆」的第三块地基：跨会话的**相关历史对话检索**。
与 FactStore（语义记忆：长期事实）不同，这里存的是「某用户过去具体说过什么」，
在未来轮次按相关性召回，让 Agent 无需用户复述历史语境。

数据布局：
    data/memory/episodes/<safe_user_id>.json
        {
          "user_id": "u_xxx",
          "created_at": 1234567.8,
          "updated_at": 1234567.8,
          "episodes": [
            {"session_id": "s1", "role": "user", "content": "...", "ts": 1234567.8},
            ...
          ]
        }

检索策略（零外部依赖、确定性）：
- 关键词重叠打分：查询词与历史内容的字面命中越多、命中词越长，权重越高
- 时间衰减：越久远的历史权重越低（半衰期可配置，默认 30 天）
- 按 user_id 隔离，杜绝跨用户串味；写入即落盘

安全：user_id 先规范化再作文件名，杜绝路径穿越（与 FactStore/UserStore 一致）。
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
    """规范化 user_id：仅保留 [a-zA-Z0-9_-]，其余折叠，防路径穿越。"""
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


# 中文/英文分词：连续汉字视为一个词；英文/数字按非字母数字切分
_TOKEN_RE = re.compile(r"[一-鿿]+|[a-z0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    """轻量分词：中文按连续汉字成词，英文按词切分。仅供关键词重叠打分用。"""
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 1]


def _jaccard(a: list[str], b: list[str]) -> float:
    """两词集合的加权重叠度（长词命中权重更高，Jaccard 归一）。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    if not inter:
        return 0.0
    # 长词更有区分度：命中词长度加权
    weighted = sum(len(w) for w in inter)
    return weighted / (weighted + max(len(sa), len(sb)))


class EpisodicStore:
    """
    按 user_id 隔离的情景记忆存储（线程安全、JSON 持久化）。

    用法:
        store = EpisodicStore()
        store.append("u_alice", "s1", "user", "我最近在做 Python 后端")
        hits = store.recall("u_alice", "Python 后端", top_k=3)
        text = store.render("u_alice", "Python 后端", top_k=3)
    """

    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        max_episodes: int = 500,
        half_life_days: float = 30.0,
    ):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "memory" / "episodes")
        self.max_episodes = max_episodes        # 单用户最多保留的片段数（防无限膨胀）
        self.half_life_days = half_life_days    # 时间衰减半衰期（天）
        self._users: dict[str, dict] = {}       # user_id -> {created_at, updated_at, episodes}
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
                # 损坏文件静默跳过（延续项目降级语义）
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
        """
        追加一条情景记忆（一轮 user/assistant 发言）。
        返回该用户当前片段总数。

        去重：连续两条完全相同的 (role, content) 不重复写入（防重放）。
        """
        content = (content or "").strip()
        if not content:
            return self.count(user_id)

        with self._lock:
            rec = self._get_or_create(user_id)
            eps = rec["episodes"]

            # 相邻去重
            if eps and eps[-1].get("role") == role and eps[-1].get("content") == content:
                return len(eps)

            eps.append({
                "session_id": session_id or "",
                "role": role,
                "content": content,
                "ts": ts if ts is not None else time.time(),
            })

            # 容量裁剪：丢弃最旧
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
        """
        检索与该用户当前 query 最相关、且较新的历史片段。

        打分 = 关键词重叠度 * 时间衰减。返回按分数降序的片段列表：
            [{"session_id","role","content","ts","score"}, ...]
        """
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

            # 时间衰减：半衰期 half_life_days 天
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
        """
        将召回的历史片段渲染为可注入 LLM 上下文的文本块。
        无相关历史时返回空字符串（调用方据此决定是否注入）。
        """
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

    def clear_user(self, user_id: str) -> None:
        """清空该用户全部情景记忆（保留用户记录）。"""
        with self._lock:
            rec = self._users.get(user_id)
            if rec:
                rec["episodes"] = []
                rec["updated_at"] = time.time()
                self._save(user_id)


# 全局单例
_episodic_store: Optional[EpisodicStore] = None


def get_episodic_store() -> EpisodicStore:
    global _episodic_store
    if _episodic_store is None:
        _episodic_store = EpisodicStore()
    return _episodic_store
