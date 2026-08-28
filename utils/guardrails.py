"""
智能搜索助手 — 护栏与安全层

基于《深入理解 AI Agent》1.2.6（护栏与安全）、2.4.7（提示注入）、3.1.8（日志脱敏）：

1. 提示注入检测：识别试图覆盖系统指令的恶意输入
2. 敏感信息脱敏：日志/输出中的 API Key、邮箱、手机号、身份证号脱敏
3. 输出护栏：检测危险/越权内容标记
4. 工具调用护栏：白名单 + 频率限制（由调用方在工具执行前检查）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 提示注入检测
# ============================================================
# 直接/间接注入的常见模式
_INJECTION_PATTERNS: list[re.Pattern] = [
    # 试图覆盖系统提示
    re.compile(r"(忽略|无视|忘记|跳过|抛弃)[^\n]{0,30}(之前|上述|以上|所有|系统)?[^\n]{0,20}(指令|提示|规则|设定|限制)", re.IGNORECASE),
    re.compile(r"(你现在是|扮演|假装是|从现在开始你是)[^\n]{0,40}", re.IGNORECASE),
    re.compile(r"(系统提示|system prompt)[^\n]{0,20}(泄露|泄漏|打印|输出|显示|告诉我)", re.IGNORECASE),
    # 编码绕过（base64 常见前缀）
    re.compile(r"(base64|aGVsbG8|IHN5c3RlbQ)", re.IGNORECASE),
    # 要求突破工具/权限边界
    re.compile(r"(绕过|突破|bypass)[^\n]{0,30}(限制|权限|过滤|安全|审查)", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> tuple[bool, list[str]]:
    """
    检测提示注入。返回 (是否命中, 命中的规则描述列表)。
    不返回敏感内容本身，仅返回命中的规则类别，避免二次泄露。
    """
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
# 需要脱敏的字段名（无论值是什么，直接掩码）
_SENSITIVE_FIELDS = {
    "api_key", "apikey", "api-key", "token", "secret", "password", "passwd",
    "authorization", "tavily_api_key", "llm_api_key", "cookie", "session",
}

# 常见敏感值模式
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
    """对文本中的敏感信息做脱敏，返回脱敏后的安全文本。"""
    if not text:
        return text
    result = text
    for _name, pattern in _SENSITIVE_VALUE_PATTERNS:
        result = pattern.sub("***", result)
    return result


def redact_kv(key: str, value: str) -> str:
    """对键值对做脱敏：若键名是敏感字段则整体掩码，否则仅对值做模式脱敏。"""
    if key.lower() in _SENSITIVE_FIELDS:
        return "***"
    return redact_text(value)


def sanitize_log(message: str) -> str:
    """日志脱敏：对任意日志字符串做敏感信息清洗。"""
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
    """
    对最终回答做输出护栏检查。
    当前仅做提示注入/明显越权的软标记，不做激进阻断，返回标记信息供上游决定。
    """
    injected, _ = detect_prompt_injection(text)
    if injected:
        return GuardResult(safe=False, blocked=False, reason="回答中包含可疑指令回显", flagged=["prompt_injection_echo"])
    return GuardResult()


def check_input_guardrails(text: str) -> GuardResult:
    """
    对用户输入做护栏检查。
    命中提示注入时阻断（blocked=True），由调用方决定降级还是拒绝。
    """
    injected, hits = detect_prompt_injection(text)
    if injected:
        return GuardResult(safe=False, blocked=True, reason="检测到疑似提示注入", flagged=hits)
    return GuardResult()


# ============================================================
# 工具调用护栏
# ============================================================
# 允许的搜索域名白名单（可选，空 = 不限，由 config.include_domains 控制）
def validate_tool_call(tool_name: str, tool_args: dict, allowed_tools: Optional[set[str]] = None) -> GuardResult:
    """
    工具调用前的护栏检查：
    - 工具名是否在允许集合内
    - search 的 query 是否为空/超长
    """
    if allowed_tools is not None and tool_name not in allowed_tools:
        return GuardResult(safe=False, blocked=True, reason=f"工具 {tool_name} 不在允许列表", flagged=["tool_not_allowed"])

    if tool_name in ("search", "tavily_search", "knowledge_search"):
        query = tool_args.get("query", "")
        if not query or not query.strip():
            return GuardResult(safe=False, blocked=True, reason="搜索 query 不能为空", flagged=["empty_query"])
        if len(query) > 500:
            return GuardResult(safe=False, blocked=True, reason="搜索 query 过长", flagged=["query_too_long"])

    return GuardResult()
