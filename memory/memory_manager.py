"""
智能搜索助手 — 记忆编排层（MemoryManager）

把两块地基串成一条 user_id 贯穿的完整链路：

    语义记忆（FactStore）   —— 长期事实，按 user_id 隔离
    情景记忆（EpisodicStore）—— 跨会话相关历史对话，按 user_id 隔离

对外提供两个核心原语：
    recall(user_id, query)   → 召回「长期事实 + 相关历史」，渲染为可注入上下文
    remember(user_id, ...)   → 写入情景记忆 + 抽取长期事实

所有方法均对 user_id 为空的情况安全降级（不落盘、返回空），
因此上层可无条件调用，不必先判空。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from memory.episodic_store import EpisodicStore, get_episodic_store
from memory.fact_extractor import extract_facts
from memory.fact_store import FactStore, get_fact_store


@dataclass
class MemoryContext:
    """一次召回的完整记忆上下文。"""
    user_id: str = ""
    facts_text: str = ""            # 语义记忆渲染文本（可注入）
    episodes_text: str = ""         # 情景记忆渲染文本（可注入）
    facts: list[dict] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.facts_text and not self.episodes_text

    def render(self) -> str:
        """渲染为最终注入 LLM 的完整记忆块；无记忆时返回空字符串。"""
        blocks: list[str] = []
        if self.facts_text:
            blocks.append(f"## 关于该用户的长期记忆\n{self.facts_text}")
        if self.episodes_text:
            blocks.append(f"## 相关历史对话片段\n{self.episodes_text}")
        return "\n\n".join(blocks)


class MemoryManager:
    """
    记忆编排层：user_id 贯穿语义 + 情景记忆的召回与写回。

    用法:
        mm = MemoryManager()
        ctx = mm.recall("u_alice", "帮我看看 Python 后端")
        mm.remember("u_alice", "s1", "user", "我用 Python 做后端")
    """

    def __init__(
        self,
        fact_store: Optional[FactStore] = None,
        episodic_store: Optional[EpisodicStore] = None,
    ):
        self.facts = fact_store or get_fact_store()
        self.episodes = episodic_store or get_episodic_store()
        self._lock = threading.Lock()  # 保证 remember 内「情景写入 + 事实写入」的原子性

    # ── 召回 ──
    def recall(
        self,
        user_id: Optional[str],
        query: str,
        max_facts: int = 20,
        max_episodes: int = 5,
    ) -> MemoryContext:
        """
        召回该用户与当前 query 相关的记忆。

        Returns:
            MemoryContext（含渲染文本 + 结构化列表）。user_id 为空时返回空上下文。
        """
        if not user_id:
            return MemoryContext()

        facts_text = self.facts.render(user_id, max_facts=max_facts)
        episodes_text = self.episodes.render(user_id, query, top_k=max_episodes)

        return MemoryContext(
            user_id=user_id,
            facts_text=facts_text,
            episodes_text=episodes_text,
            facts=self.facts.get_facts(user_id),
            episodes=self.episodes.recall(user_id, query, top_k=max_episodes),
        )

    # ── 写回 ──
    def remember(
        self,
        user_id: Optional[str],
        session_id: str,
        role: str,
        content: str,
        extract_llm: bool = False,
    ) -> dict:
        """
        写回记忆：情景记忆直接落盘；用户发言额外抽取长期事实。

        Returns:
            {"episodes": int, "facts_added": int}
        """
        if not user_id:
            return {"episodes": 0, "facts_added": 0}

        with self._lock:
            # 1. 情景记忆（所有角色都记录，用于跨会话召回语境）
            self.episodes.append(user_id, session_id, role, content)

            # 2. 语义记忆（仅用户发言抽取长期事实）
            facts_added = 0
            if role == "user":
                facts = extract_facts_sync(content, use_llm=extract_llm)
                if facts:
                    self.facts.upsert(user_id, facts)
                    facts_added = len(facts)

            return {"episodes": self.episodes.count(user_id), "facts_added": facts_added}

    async def aremember(
        self,
        user_id: Optional[str],
        session_id: str,
        role: str,
        content: str,
        extract_llm: bool = False,
    ) -> dict:
        """
        remember 的异步版本：支持 LLM 兜底抽取（在事件循环内 await）。
        其余语义与 remember 完全一致。
        """
        if not user_id:
            return {"episodes": 0, "facts_added": 0}

        with self._lock:
            self.episodes.append(user_id, session_id, role, content)

            facts_added = 0
            if role == "user":
                facts = await extract_facts(content, use_llm=extract_llm)
                if facts:
                    self.facts.upsert(user_id, facts)
                    facts_added = len(facts)

            return {"episodes": self.episodes.count(user_id), "facts_added": facts_added}


# ============================================================
# 同步事实抽取封装（供非异步上下文使用）
# ============================================================
def extract_facts_sync(text: str, use_llm: bool = False) -> list[dict]:
    """同步抽取事实（默认纯规则，不调 LLM；use_llm 仅在异步环境可用）。"""
    from memory.fact_extractor import extract_facts_rules

    return extract_facts_rules(text)


# 全局单例
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
