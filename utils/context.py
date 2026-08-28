"""
智能搜索助手 — 上下文压缩策略

基于《深入理解 AI Agent》2.7：
- 分层压缩：历史消息按「完整保留近期 + 摘要压缩远期」分层，控制总 token
- 前缀稳定化：系统提示与历史顺序固定，使提供商可命中 prompt cache / KV cache
- 工具结果裁剪：搜索结果只保留模型需要的摘要，避免全文灌入上下文

原则（2.7.7）：隔离优于压缩——跨轮仅保留必要摘要，不重复注入完整旧内容。
"""
from __future__ import annotations

from typing import Optional

from config import get_agent_config
from utils.helpers import count_tokens


# ============================================================
# 历史分层压缩
# ============================================================
def compress_history(
    history: list[dict[str, str]],
    max_turns: Optional[int] = None,
    recent_full_turns: int = 2,
) -> list[dict[str, str]]:
    """
    分层压缩对话历史：
    - 最近 `recent_full_turns` 轮完整保留（模型最需要近期语境）
    - 更早的历史压缩为单条摘要消息（减少 token 与 KV 占用）

    Returns:
        压缩后的消息列表（稳定顺序，利于 prompt cache）
    """
    if not history:
        return []

    cfg = get_agent_config()
    max_turns = max_turns or cfg.max_history_turns
    max_messages = max_turns * 2

    # 已不超限，直接返回（保持原顺序稳定）
    if len(history) <= max_messages:
        return list(history)

    # 分层：近期完整保留，远期压缩
    recent_messages = history[-(recent_full_turns * 2):]
    older_messages = history[: -(recent_full_turns * 2)]

    # 远期压缩为一条摘要（仅保留用户问题，答案不重复注入）
    user_questions = [m["content"] for m in older_messages if m.get("role") == "user"]
    if user_questions:
        summary = "（早前对话摘要）" + "；".join(q[:40] for q in user_questions)
        return [{"role": "user", "content": summary}] + recent_messages

    return recent_messages


# ============================================================
# 工具结果裁剪
# ============================================================
def trim_tool_result(text: str, max_tokens: int = 800) -> str:
    """
    对单个工具返回的文本做 token 级裁剪，避免搜索结果全文灌入上下文。
    保留头部（通常含最相关结果），截断尾部并加省略标记。
    """
    if not text:
        return text
    if count_tokens(text) <= max_tokens:
        return text
    # 简单按比例截断：中文约 1.5 字符/token，英文约 4 字符/token
    approx_chars = max_tokens * 2
    return text[:approx_chars] + "\n…（内容过长已截断）"


# ============================================================
# 前缀稳定化的消息构建
# ============================================================
def build_stable_messages(
    system_prompt: str,
    history: Optional[list[dict[str, str]]],
    user_query: str,
    recent_full_turns: int = 2,
) -> list[dict[str, str]]:
    """
    构建前缀稳定的消息列表：
    顺序固定为 [system] + [压缩后的历史] + [user]，
    历史顺序确定，使提供商对「system + 历史」前缀可复用缓存。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(compress_history(history, recent_full_turns=recent_full_turns))
    messages.append({"role": "user", "content": user_query})
    return messages
