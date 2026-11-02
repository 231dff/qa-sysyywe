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
import warnings
from dataclasses import dataclass, field
from typing import Optional

from memory.episodic_store import EpisodicStore, get_episodic_store
from memory.fact_extractor import extract_facts
from memory.fact_store import FactStore, get_fact_store


@dataclass
class MemoryContext:
    """一次召回的完整记忆上下文。"""
    user_id: str = ""
    facts_text: str = ""
    episodes_text: str = ""
    facts: list[dict] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.facts_text and not self.episodes_text

    def render(self) -> str:
        blocks: list[str] = []
        if self.facts_text:
            blocks.append(f"## 关于该用户的长期记忆\n{self.facts_text}")
        if self.episodes_text:
            blocks.append(f"## 相关历史对话片段\n{self.episodes_text}")
        return "\n\n".join(blocks)


class MemoryManager:
    """
    记忆编排层：user_id 贯穿语义 + 情景记忆的召回与写回。
    """

    def __init__(
        self,
        fact_store: Optional[FactStore] = None,
        episodic_store: Optional[EpisodicStore] = None,
    ):
        self.facts = fact_store or get_fact_store()
        self.episodes = episodic_store or get_episodic_store()
        self._lock = threading.Lock()

    # ── 召回 ──
    def recall(
        self,
        user_id: Optional[str],
        query: str,
        max_facts: int = 20,
        max_episodes: int = 5,
    ) -> MemoryContext:
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

    # ── 同步写回（仅规则抽取） ──
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

        ★ 修复 5a：同步路径只做规则抽取。extract_llm=True 时不再静默失效，
          而是打 warning 并降级到规则抽取；如需 LLM 抽取请改用 aremember。
        """
        if not user_id:
            return {"episodes": 0, "facts_added": 0}

        with self._lock:
            self.episodes.append(user_id, session_id, role, content)

            facts_added = 0
            if role == "user":
                facts = extract_facts_sync(content, use_llm=extract_llm)
                if facts:
                    self.facts.upsert(user_id, facts)
                    facts_added = len(facts)

            return {
                "episodes": self.episodes.count(user_id),
                "facts_added": facts_added,
            }

    # ── 异步写回（支持 LLM 抽取） ──
    async def aremember(
        self,
        user_id: Optional[str],
        session_id: str,
        role: str,
        content: str,
        extract_llm: bool = False,
    ) -> dict:
        """
        remember 的异步版本：支持 LLM 兜底抽取。

        ★ 修复 5b：await 全部放在锁外，锁区内零 await，
          避免 threading.Lock 在事件循环里阻塞其他协程。
        """
        if not user_id:
            return {"episodes": 0, "facts_added": 0}

        # 1) 锁外 await：异步事实抽取（可能调 LLM，耗时几十秒）
        facts: list[dict] = []
        if role == "user":
            try:
                facts = await extract_facts(content, use_llm=extract_llm)
            except Exception as exc:
                # 抽取失败不阻断主流程，降级为不写事实
                warnings.warn(f"[MemoryManager] aremember 事实抽取失败: {exc}")
                facts = []

        # 2) 锁内原子写入：零 await，不阻塞事件循环
        with self._lock:
            self.episodes.append(user_id, session_id, role, content)
            if facts:
                self.facts.upsert(user_id, facts)

        return {
            "episodes": self.episodes.count(user_id),
            "facts_added": len(facts),
        }


# ============================================================
# 同步事实抽取封装（供非异步上下文使用）
# ============================================================
def extract_facts_sync(text: str, use_llm: bool = False) -> list[dict]:
    """
    同步抽取事实（只走规则）。

    ★ 修复 5a：use_llm=True 时显式打 warning 并降级，不再静默忽略。
       真正需要 LLM 抽取请改用 MemoryManager.aremember（async）。
    """
    from memory.fact_extractor import extract_facts_rules

    if use_llm:
        warnings.warn(
            "[MemoryManager] extract_facts_sync 不支持 use_llm=True，已降级为规则抽取。"
            "如需 LLM 抽取，请改用 aremember（异步）。",
            RuntimeWarning,
            stacklevel=2,
        )

    return extract_facts_rules(text)


# 全局单例
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager