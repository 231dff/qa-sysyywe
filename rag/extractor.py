"""
智能搜索助手 — 知识图谱 LLM 抽取器

用 LLM 从自然语言文本中抽取实体与关系，供知识图谱层（rag/graph.py）建图。

复用现有基础设施：
- utils/http_client.get_http_client：连接池复用
- config.get_api_config + APIConfig.supports_response_format：多平台 provider 降级
  （DeepSeek 不支持 response_format=json_object）
- utils/metrics.record_error / record_llm_call / record_tokens

失败兜底：LLM 不可用 / 超时 / 解析失败时返回空 {entities:[], relations:[]}，
不抛异常、不阻断自然语言记忆写入（降级为「仅 NL 记忆，无图谱」）。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from config import get_api_config, get_agent_config
from utils.http_client import get_http_client
from utils.metrics import record_error, record_extraction, record_llm_call, record_tokens

# ============================================================
# 抽取提示词
# ============================================================
EXTRACTION_SYSTEM_PROMPT = """你是一个信息抽取专家。从给定文本中抽取**实体**与**关系**，用于构建知识图谱。

## 抽取规则
1. 实体：人名、机构、地点、病症、药品、法律条文、概念等有明确指代的对象
   - name: 实体规范名（去口语，用标准称谓）
   - type: 实体类型（如 person / organization / disease / drug / law / place / concept）
   - aliases: 文本中出现的别名/简称/昵称列表
   - attributes: 实体的关键属性（key-value，如年龄、身份、编号）
2. 关系：实体之间的语义关系
   - source / target: 两个实体的 name（必须是上面列出的实体）
   - type: 关系类型（如 father_of / spouse_of / suffered_from / prescribes / prosecuted / works_at）
3. 只抽取文本中**明确出现**的信息，不要臆测补全
4. 无实体或无关系时，对应数组返回空 []

## 输出格式（严格遵守 JSON）
{
  "entities": [
    {"name": "张三", "type": "person", "aliases": ["三哥"], "attributes": {"age": "40"}}
  ],
  "relations": [
    {"source": "张三", "target": "张小明", "type": "father_of"}
  ]
}"""


async def extract_entities_relations(text: str) -> dict:
    """
    用 LLM 从文本抽取实体与关系。

    Args:
        text: 待抽取的自然语言文本

    Returns:
        {"entities": [{name,type,aliases,attributes}...],
         "relations": [{source,target,type}...]}
        失败时返回 {"entities": [], "relations": []}
    """
    if not text or not text.strip():
        return {"entities": [], "relations": []}

    api_cfg = get_api_config()
    agent_cfg = get_agent_config()

    user_message = f"""## 待抽取文本
{text[:4000]}

请输出 JSON:"""

    request_body = {
        "model": api_cfg.llm_model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }
    if api_cfg.supports_response_format:
        request_body["response_format"] = {"type": "json_object"}

    start = time.perf_counter()
    try:
        async with get_http_client(timeout=30.0) as client:
            resp = await client.post(
                f"{api_cfg.llm_api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_cfg.llm_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)

        elapsed = time.perf_counter() - start
        record_llm_call("extract", elapsed)
        usage = data.get("usage", {})
        record_tokens(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )
        record_extraction("success")

        return {
            "entities": _coerce_entities(parsed.get("entities", [])),
            "relations": _coerce_relations(parsed.get("relations", [])),
        }

    except Exception as exc:
        elapsed = time.perf_counter() - start
        record_llm_call("extract", elapsed)
        record_error("llm_error")
        record_extraction("failure")
        if agent_cfg.verbose:
            print(f"[实体抽取失败] {exc}, 降级为仅自然语言记忆")
        return {"entities": [], "relations": []}


# ============================================================
# 输出规范化（防御 LLM 输出格式漂移）
# ============================================================
def _coerce_entities(raw: list) -> list[dict]:
    entities: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        entities.append({
            "name": name,
            "type": str(item.get("type") or "entity"),
            "aliases": [a for a in (item.get("aliases") or []) if isinstance(a, str)],
            "attributes": item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
        })
    return entities


def _coerce_relations(raw: list) -> list[dict]:
    relations: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source = (item.get("source") or "").strip()
        target = (item.get("target") or "").strip()
        rtype = str(item.get("type") or "related_to")
        if not source or not target:
            continue
        relations.append({"source": source, "target": target, "type": rtype})
    return relations
