"""
智能搜索助手 — LLM 工厂（Provider 路由版）

前端传 provider + model，后端从 ProviderStore 解析出 base_url + api_key + model，
再构建 ChatOpenAI 或 ChatDeepSeek。

★ 路由规则：
  - provider 的 api 字段为 "openai-completions" 且 model 含 "reasoner" → ChatDeepSeek
  - 其他 → ChatOpenAI
  这不是维护映射表，而是按协议类型和模型名做一次极轻量的适配器选择。
"""
from __future__ import annotations

from typing import Optional

from langchain_openai import ChatOpenAI

try:
    from langchain_deepseek import ChatDeepSeek
    _HAS_CHAT_DEEPSEEK = True
except ImportError:
    ChatDeepSeek = None
    _HAS_CHAT_DEEPSEEK = False


def build_llm(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float = 0.3,
    streaming: bool = True,
):
    """
    根据 model 名选择适配器：
    - 含 "reasoner" 且 langchain-deepseek 可用 → ChatDeepSeek（自动回传 reasoning_content）
    - 其他 → ChatOpenAI
    """
    model_lower = (model or "").lower()

    if "reasoner" in model_lower and _HAS_CHAT_DEEPSEEK:
        try:
            return ChatDeepSeek(
                model=model,
                base_url=base_url,
                api_key=api_key,
                temperature=temperature,
                streaming=streaming,
                max_retries=3,
            )
        except Exception as e:
            print(f"[LLMFactory] ChatDeepSeek 实例化失败，回退 ChatOpenAI: {e}")

    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        streaming=streaming,
        max_retries=3,
    )