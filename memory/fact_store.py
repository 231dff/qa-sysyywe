"""
智能搜索助手 — 语义记忆（长期事实存储）

解决「金鱼记忆」的第二块地基：从跨轮对话中提取用户长期事实
（如「我叫小明」「我住在北京」「我用 Python 做后端」），以 key-value 形式
按 user_id 隔离持久化，并在未来轮次注入上下文，使 Agent 无需用户重复交代。

数据布局：
    data/memory/facts/<safe_user_id>.json
        {
          "user_id": "u_xxx",
          "created_at": 1234567.8,
          "updated_at": 1234567.8,
          "facts": {
            "<key>": {"value": "...", "confidence": 0.9, "updated_at": 1234567.8},
            ...
          }
        }

设计要点：
- key 为事实的规范化标签（如「名字」「居住城市」），同一 key 再次出现时
  按置信度择优保留（高置信度覆盖低置信度，低置信度不降级已有高置信度）。
- 安全：user_id 先规范化再作文件名，杜绝路径穿越。
- 线程安全：全局锁保护内存态，写入即落盘。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from config import PROJECT_ROOT


def _safe_user_id(user_id: str) -> str:
    """规范化 user_id：仅保留 [a-zA-Z0-9_-]，其余折叠，防路径穿越。"""
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in user_id)
    return safe[:64] or "anon"


class FactStore:
    """
    按 user_id 隔离的长期事实存储（线程安全、JSON 持久化）。

    用法:
        store = FactStore()
        store.upsert("u_alice", [{"key": "名字", "value": "小明", "confidence": 0.9}])
        store.get_facts("u_alice")  # -> [{"key":"名字","value":"小明",...}, ...]
    """

    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = str(persist_dir or PROJECT_ROOT / "data" / "memory" / "facts")
        self._users: dict[str, dict] = {}  # user_id -> {created_at, updated_at, facts}
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
                    "facts": data.get("facts", {}),
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
            "facts": rec.get("facts", {}),
        }
        self._path(user_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _get_or_create(self, user_id: str) -> dict:
        if user_id not in self._users:
            self._users[user_id] = {
                "created_at": time.time(),
                "updated_at": time.time(),
                "facts": {},
            }
        return self._users[user_id]

    # ── 写入 ──
    def upsert(self, user_id: str, facts: list[dict]) -> int:
        """
        合并写入一批事实。每个 dict 含 key/value/confidence(可选)。
        返回最终事实总数。

        合并规则：同一 key 若已存在，
        - 新置信度 >= 旧置信度 → 覆盖
        - 新置信度 < 旧置信度 → 保留旧值（防止低置信度漂移覆盖高置信度）
        """
        with self._lock:
            rec = self._get_or_create(user_id)
            changed = False
            for f in facts:
                key = (f.get("key") or "").strip()
                if not key:
                    continue
                value = (f.get("value") or "").strip()
                if not value:
                    continue
                conf = float(f.get("confidence", 0.5))
                existing = rec["facts"].get(key)
                if existing is not None and existing.get("confidence", 0.0) > conf:
                    continue  # 已有更高置信度，不降级
                rec["facts"][key] = {
                    "value": value,
                    "confidence": conf,
                    "updated_at": time.time(),
                }
                changed = True
            if changed:
                rec["updated_at"] = time.time()
                self._save(user_id)
            return len(rec["facts"])

    # ── 读取 ──
    def get_facts(self, user_id: str) -> list[dict]:
        """返回该用户的全部事实列表（含 key/value/confidence/updated_at）。"""
        with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return []
            return [
                {"key": k, "value": v.get("value", ""), "confidence": v.get("confidence", 0.0),
                 "updated_at": v.get("updated_at", 0.0)}
                for k, v in rec.get("facts", {}).items()
            ]

    def render(self, user_id: str, max_facts: int = 20) -> str:
        """
        将用户事实渲染为可注入 LLM 上下文的文本块（按置信度降序取前 N 条）。
        无事实时返回空字符串（调用方据此决定是否注入）。
        """
        facts = self.get_facts(user_id)
        if not facts:
            return ""
        facts.sort(key=lambda f: f.get("confidence", 0.0), reverse=True)
        lines = [f"- {f['key']}: {f['value']}" for f in facts[:max_facts]]
        return "\n".join(lines)

    def count(self, user_id: str) -> int:
        with self._lock:
            rec = self._users.get(user_id)
            return len(rec.get("facts", {})) if rec else 0

    # ── 删除 ──
    def delete(self, user_id: str, key: str) -> bool:
        """删除单条事实，返回是否删除成功。"""
        with self._lock:
            rec = self._users.get(user_id)
            if not rec or key not in rec.get("facts", {}):
                return False
            del rec["facts"][key]
            rec["updated_at"] = time.time()
            self._save(user_id)
            return True

    def clear_user(self, user_id: str) -> None:
        """清空该用户全部事实（保留用户记录）。"""
        with self._lock:
            rec = self._users.get(user_id)
            if rec:
                rec["facts"] = {}
                rec["updated_at"] = time.time()
                self._save(user_id)


# 全局单例
_fact_store: Optional[FactStore] = None


def get_fact_store() -> FactStore:
    global _fact_store
    if _fact_store is None:
        _fact_store = FactStore()
    return _fact_store
