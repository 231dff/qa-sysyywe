"""
智能搜索助手 — 上下文压缩策略 (LangChain 1.x 兼容)

compress_history 返回 list[dict]，在 react.py._to_messages 中被转成
SystemMessage / HumanMessage / AIMessage。1.x 中 BaseMessage 构造方式不变，
此文件不需要修改。
"""
from __future__ import annotations

from typing import Optional

from config import get_agent_config
from utils.helpers import count_tokens


def compress_history(
    history: list[dict[str, str]],
    max_turns: Optional[int] = None,
    recent_full_turns: int = 2,
) -> list[dict[str, str]]:
    if not history:
        return []

    cfg = get_agent_config()
    max_turns = max_turns or cfg.max_history_turns
    max_messages = max_turns * 2

    if len(history) <= max_messages:
        return list(history)

    recent_messages = history[-(recent_full_turns * 2):]
    older_messages = history[: -(recent_full_turns * 2)]

    user_questions = [m["content"] for m in older_messages if m.get("role") == "user"]
    if user_questions:
        summary = "[历史摘要] 早前用户提过：" + "；".join(q[:40] for q in user_questions)
        return [{"role": "system", "content": summary}] + recent_messages

    return recent_messages


def trim_tool_result(text: str, max_tokens: int = 800) -> str:
    if not text:
        return text
    if count_tokens(text) <= max_tokens:
        return text
    approx_chars = max_tokens * 2
    return text[:approx_chars] + "\n…（内容过长已截断）"


def build_stable_messages(
    system_prompt: str,
    history: Optional[list[dict[str, str]]],
    user_query: str,
    recent_full_turns: int = 2,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(compress_history(history, recent_full_turns=recent_full_turns))
    messages.append({"role": "user", "content": user_query})
    return messages