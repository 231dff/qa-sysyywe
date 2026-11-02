"""
智能搜索助手 — 请求级 LLM 配置上下文

把「当前请求生效的 LLM 配置」做成 ContextVar，供 agent/tools.py 与
tools/registry.py 共享，打破二者的循环 import。
"""
from __future__ import annotations

from contextvars import ContextVar

_llm_api_key: ContextVar[str] = ContextVar("llm_api_key", default="")
_llm_api_base: ContextVar[str] = ContextVar("llm_api_base", default="")
_llm_model: ContextVar[str] = ContextVar("llm_model", default="")


def set_llm_context(api_key: str, api_base: str, model: str) -> None:
    """由主 Agent 在进入 ReAct 循环前注入当前生效的 LLM 配置。"""
    _llm_api_key.set(api_key or "")
    _llm_api_base.set(api_base or "")
    _llm_model.set(model or "")


def get_llm_context() -> dict[str, str]:
    """读取当前请求的 LLM 配置。未设置时返回空字符串，调用方据此回退 .env。"""
    return {
        "api_key": _llm_api_key.get(),
        "api_base": _llm_api_base.get(),
        "model": _llm_model.get(),
    }


def get_llm_api_key() -> str:
    return _llm_api_key.get()


def get_llm_api_base() -> str:
    return _llm_api_base.get()


def get_llm_model() -> str:
    return _llm_model.get()