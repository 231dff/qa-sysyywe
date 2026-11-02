"""
智能搜索助手 — 语义事实抽取器（fact_extractor）

从用户消息中抽取「长期事实」（语义记忆的原料），产出与 FactStore.upsert
兼容的 {"key","value","confidence"} 列表。

★ LLM 配置：从 utils.llm_context 读取请求级配置，回退到 .env
★ 超时契约：共享客户端模式下，client.post 必须显式传 timeout=
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from config import get_agent_config, get_api_config
from utils.llm_context import get_llm_context

# ============================================================
# 规则模式
# ============================================================
_RULE_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("名字", re.compile(r"我(?:的)?(?:名字|姓名)(?:是|叫)[：:\s]*([一-龥A-Za-z]{2,10})"), 0.85),
    ("名字", re.compile(r"我叫[：:\s]*([一-龥A-Za-z]{2,10})"), 0.85),
    ("名字", re.compile(r"称呼我[：:\s]*(?:为|叫)[：:\s]*([一-龥A-Za-z]{2,10})"), 0.8),
    ("居住城市", re.compile(r"我(?:现在)?(?:住在|居住|住|在)[：:\s]*([一-龥]{2,12}(?:市|省|县)?)"), 0.7),
    ("居住城市", re.compile(r"我(?:来自|从)[：:\s]*([一-龥]{2,12}(?:市|省|县)?)"), 0.6),
    ("职业", re.compile(r"我是(?:一名|一个|个)?([一-龥A-Za-z]{2,16}(?:工程师|医生|教师|设计师|产品经理|学生|律师|程序员|分析师|运营|架构师))"), 0.75),
    ("职业", re.compile(r"我(?:从事|做|干)[：:\s]*([一-龥A-Za-z]{2,16}(?:开发|工程|设计|产品|运营|教育|医疗|法律|金融|数据))"), 0.65),
    ("技术栈", re.compile(r"我(?:主要)?(?:用|使用|在?用|在用)[：:\s]*([A-Za-z+#.\d]{2,20}(?:/[A-Za-z+#.\d]{2,20})?)"), 0.6),
    ("技术栈", re.compile(r"(?:主要)?(?:技术栈|栈)是[：:\s]*([A-Za-z+#.,\s\d]{2,30})"), 0.7),
    ("技术栈", re.compile(r"(?:用|使用|在用|主要用)[：:\s]*([A-Za-z+#.\d]{2,20}(?:/[A-Za-z+#.\d]{2,20})?)"), 0.5),
    ("年龄", re.compile(r"我(?:今年|已经)?(\d{1,3})岁"), 0.8),
    ("偏好", re.compile(r"我(?:比较)?(?:喜欢|偏好|习惯)[：:\s]*([一-龥A-Za-z0-9、，, ]{2,20})"), 0.55),
]

_VALUE_CLEAN_RE = re.compile(r"[，。！？,.!?；;、\s]+$")


def _clean_value(value: str) -> str:
    value = (value or "").strip()
    value = _VALUE_CLEAN_RE.sub("", value)
    return value[:40]


def extract_facts_rules(text: str) -> list[dict]:
    if not text or not text.strip():
        return []

    best: dict[str, dict] = {}
    for label, pattern, conf in _RULE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        value = _clean_value(m.group(1))
        if not value:
            continue
        if not re.search(r"[0-9A-Za-z一-龥]", value):
            continue
        existing = best.get(label)
        if existing is None or conf > existing["confidence"]:
            best[label] = {"key": label, "value": value, "confidence": conf}

    return list(best.values())


# ============================================================
# 可选 LLM best-effort 抽取
# ============================================================
EXTRACT_FACT_SYSTEM_PROMPT = """你是一个用户长期事实抽取助手。从用户发言中抽取**可复用的长期属性**，
只抽取用户明确自我陈述的事实（名字、居住城市、职业、技术栈、年龄、偏好等），
不要抽取一次性请求或情境性内容。

## 输出格式（严格遵守 JSON）
{
  "facts": [
    {"key": "名字", "value": "小明", "confidence": 0.9}
  ]
}

规则：
1. key 用规范化中文标签（名字/居住城市/职业/技术栈/年龄/偏好/其他）
2. confidence 0-1 之间的浮点数，越高越确定
3. 没有可抽取的事实返回 {"facts": []}
4. 只基于文本，不要臆测补全"""


def _resolve_llm() -> dict[str, str]:
    cfg = get_api_config()
    ctx = get_llm_context()
    model = ctx["model"] or cfg.llm_model
    base = ctx["api_base"] or cfg.llm_api_base
    key = ctx["api_key"] or cfg.llm_api_key.get_secret_value()

    supports_response_format = cfg.supports_response_format
    if "deepseek" in model.lower():
        supports_response_format = False

    return {
        "model": model,
        "base": base,
        "key": key,
        "supports_response_format": supports_response_format,
    }


async def extract_facts_llm(text: str) -> list[dict]:
    if not text or not text.strip():
        return []

    agent_cfg = get_agent_config()
    llm = _resolve_llm()

    user_message = f"""## 用户发言
{text[:1000]}

请输出 JSON:"""

    request_body = {
        "model": llm["model"],
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": EXTRACT_FACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }
    if llm["supports_response_format"]:
        request_body["response_format"] = {"type": "json_object"}

    try:
        from utils.http_client import get_http_client

        async with get_http_client(timeout=30.0) as client:
            resp = await client.post(
                f"{llm['base']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm['key']}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
    except Exception as exc:
        if agent_cfg.verbose:
            print(f"[事实抽取(LLM)失败] {exc}, 回退到规则抽取")
        return []

    facts: list[dict] = []
    for item in parsed.get("facts", []) or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or "").strip()
        value = _clean_value(item.get("value") or "")
        if not key or not value:
            continue
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        facts.append({"key": key, "value": value, "confidence": max(0.0, min(1.0, conf))})
    return facts


async def extract_facts(text: str, use_llm: bool = False) -> list[dict]:
    facts = extract_facts_rules(text)

    if use_llm:
        try:
            llm_facts = await extract_facts_llm(text)
        except Exception:
            llm_facts = []
        merged: dict[str, dict] = {f["key"]: f for f in facts}
        for f in llm_facts:
            existing = merged.get(f["key"])
            if existing is None or f["confidence"] >= existing["confidence"]:
                merged[f["key"]] = f
        facts = list(merged.values())

    return facts