"""
智能搜索助手 — 护栏与安全层

★ 修 21：_SENSITIVE_FIELDS 移除 "session"，
   session_id 是排障关键字段，不应被当成敏感值整体掩码。
   保留 session_token / session_secret / session_key 等真正的敏感字段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 提示注入检测
# ============================================================
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(忽略|无视|忘记|忘掉|跳过|抛弃)[^\n]{0,30}(之前|上述|以上|所有|系统)?[^\n]{0,20}(指令|提示|规则|设定|限制)", re.IGNORECASE),
    re.compile(r"(你现在是|扮演|假装是|假装你是|从现在开始你是)[^\n]{0,40}", re.IGNORECASE),
    re.compile(r"(系统提示|system prompt)[^\n]{0,20}(泄露|泄漏|打印|输出|显示|告诉我)", re.IGNORECASE),
    re.compile(r"(泄露|泄漏|打印|输出|显示|告诉我)[^\n]{0,20}(系统提示|system prompt)", re.IGNORECASE),
    re.compile(r"(aGVsbG8|IHN5c3RlbQ)", re.IGNORECASE),
    re.compile(r"base64[^\n]{0,10}(解码|decode|执行|还原|绕过)", re.IGNORECASE),
    re.compile(r"(绕过|突破|bypass)[^\n]{0,30}(限制|权限|过滤|安全|审查)", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> tuple[bool, list[str]]:
    if not text:
        return False, []
    hits: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            hits.append(pattern.pattern[:40])
    return bool(hits), hits


# ============================================================
# 敏感信息脱敏
# ============================================================
# ★ 修 21：移除 "session"；只保留真正的凭据/令牌字段
_SENSITIVE_FIELDS = {
    "api_key", "apikey", "api-key", "token", "secret", "password", "passwd",
    "authorization", "tavily_api_key", "llm_api_key", "cookie",
    "session_token", "session_secret", "session_key",
}

_SENSITIVE_VALUE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("id_card", re.compile(r"\b\d{17}[\dXx]\b")),
    ("api_key", re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b")),
    ("openai_key", re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b")),
    ("tavily_key", re.compile(r"\b(tvly-[A-Za-z0-9_\-]{8,})\b")),
    ("aws_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
]


def redact_text(text: str) -> str:
    if not text:
        return text
    result = text
    for _name, pattern in _SENSITIVE_VALUE_PATTERNS:
        result = pattern.sub("***", result)
    return result


def redact_kv(key: str, value: str) -> str:
    if key.lower() in _SENSITIVE_FIELDS:
        return "***"
    return redact_text(value)


def sanitize_log(message: str) -> str:
    return redact_text(message)


# ============================================================
# 输出护栏
# ============================================================
@dataclass
class GuardResult:
    safe: bool = True
    blocked: bool = False
    reason: str = ""
    flagged: list[str] = field(default_factory=list)


def check_output_guardrails(text: str) -> GuardResult:
    injected, _ = detect_prompt_injection(text)
    if injected:
        return GuardResult(safe=False, blocked=False, reason="回答中包含可疑指令回显", flagged=["prompt_injection_echo"])
    return GuardResult()


def check_input_guardrails(text: str) -> GuardResult:
    injected, hits = detect_prompt_injection(text)
    if injected:
        return GuardResult(safe=False, blocked=True, reason="检测到疑似提示注入", flagged=hits)
    return GuardResult()


# ============================================================
# 工具调用护栏
# ============================================================
def validate_tool_call(tool_name: str, tool_args: dict, allowed_tools: Optional[set[str]] = None) -> GuardResult:
    if allowed_tools is not None and tool_name not in allowed_tools:
        return GuardResult(safe=False, blocked=True, reason=f"工具 {tool_name} 不在允许列表", flagged=["tool_not_allowed"])

    if tool_name in ("search", "tavily_search", "knowledge_search", "graph_search"):
        query = tool_args.get("query", "")
        if not query or not query.strip():
            return GuardResult(safe=False, blocked=True, reason="搜索 query 不能为空", flagged=["empty_query"])
        if len(query) > 500:
            return GuardResult(safe=False, blocked=True, reason="搜索 query 过长", flagged=["query_too_long"])

    return GuardResult()