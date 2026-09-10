"""
智能搜索助手 — 全局配置管理
基于 pydantic-settings，支持 .env 环境变量注入

多平台适配：通过 LLM_PROVIDER 环境变量选择预设（openai / qwen / deepseek），
或直接设置 LLM_API_BASE + LLM_MODEL 覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent


# ============================================================
# 多平台 Provider 预设
# ============================================================
# 每个预设定义：endpoint、默认模型、是否支持 response_format(json)、是否支持 embedding
PROVIDER_PRESETS: dict[str, dict] = {
    "openai": {
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "supports_response_format": True,
        "supports_embedding": True,
    },
    "qwen": {
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "supports_response_format": True,
        "supports_embedding": True,
    },
    "deepseek": {
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        # DeepSeek 不支持 response_format=json_object 与 embedding 端点
        "supports_response_format": False,
        "supports_embedding": False,
    },
}


def get_provider_preset(provider: str) -> dict:
    """获取 provider 预设，未知 provider 回退到 openai 结构但不覆盖用户显式配置"""
    return PROVIDER_PRESETS.get(provider.lower(), {})


# ============================================================
# 外部 API 配置
# ============================================================
class APIConfig(BaseSettings):
    """第三方 API 密钥与端点"""

    # 多平台 provider 选择（可选；显式设置 llm_api_base/model 时优先）
    llm_provider: str = Field(
        default="",
        description="LLM 提供商预设: openai | qwen | deepseek（空则用 llm_api_base 直连）",
    )

    # LLM API (兼容 OpenAI 协议)
    llm_api_key: SecretStr = Field(
        default=SecretStr("sk-your-api-key"),
        description="LLM API Key",
    )
    llm_api_base: str = Field(
        default="https://api.openai.com/v1",
        description="LLM API Base URL",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="默认 LLM 模型名称",
    )
    llm_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="LLM 推理温度",
    )

    # Tavily 搜索 API
    tavily_api_key: SecretStr = Field(
        default=SecretStr("tvly-your-api-key"),
        description="Tavily Search API Key",
    )
    tavily_api_base: str = Field(
        default="https://api.tavily.com",
        description="Tavily API Base URL",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 平台能力探测 ──
    @property
    def resolved_provider(self) -> str:
        """返回实际生效的 provider 名称（未显式指定时为 '' 表示自定义）"""
        return self.llm_provider.lower() if self.llm_provider else ""

    @property
    def supports_response_format(self) -> bool:
        """当前 provider 是否支持 response_format=json_object"""
        preset = get_provider_preset(self.resolved_provider)
        if preset:
            return preset.get("supports_response_format", True)
        # 未指定 provider 时，保守假设支持（OpenAI 兼容端点普遍支持）
        return True

    @property
    def supports_embedding(self) -> bool:
        """当前 provider 是否支持 embedding 端点（DeepSeek 不支持）"""
        preset = get_provider_preset(self.resolved_provider)
        if preset:
            return preset.get("supports_embedding", False)
        return False


# ============================================================
# TencentDB-Agent-Memory（TDAI）可选外部记忆侧车
# ============================================================
class TdaiConfig(BaseSettings):
    """TencentDB-Agent-Memory 独立 Gateway 连接配置（可选增强记忆）。

    TencentDB-Agent-Memory 是 Node 侧的分层长期记忆侧车（L0 对话 → L1 原子事实
    → L2 场景 → L3 人设），暴露纯 HTTP 端点，可脱离 OpenClaw 独立运行。

    通过 TDAI_ENABLED 开启后，记忆召回/写回可切换到该侧车；未开启（默认）时，
    本项目沿用自研 FactStore + EpisodicStore，行为完全不变。
    """

    tdai_enabled: bool = Field(
        default=False,
        description="是否启用 TencentDB-Agent-Memory 分层记忆侧车",
    )
    tdai_gateway_url: str = Field(
        default="http://127.0.0.1:8420",
        description="TDAI Gateway 基地址（不含路径）",
    )
    tdai_api_key: str = Field(
        default="",
        description="TDAI Gateway 鉴权 Bearer Key（与 TDAI_GATEWAY_API_KEY 对齐，空=无鉴权）",
    )
    tdai_timeout: float = Field(
        default=30.0,
        gt=0.0,
        description="TDAI Gateway 单次请求超时（秒）",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# ============================================================
# Agent 行为配置
# ============================================================
class AgentConfig:
    """Agent 运行时参数"""

    # ── 搜索 ──
    max_search_results: int = 10           # Tavily 单次最大返回条数
    search_depth: str = "advanced"         # "basic" | "advanced"
    search_timeout: float = 15.0           # 搜索 API 超时 (秒)
    include_domains: list[str] = []        # 白名单域名 (空 = 不限)
    exclude_domains: list[str] = []        # 黑名单域名

    # ── 上下文裁剪 ──
    top_k_fragments: int = 5               # 相关性打分后保留的 Top-K 片段
    max_context_tokens: int = 3000         # 单轮最大上下文 token 数
    dedup_window: int = 50                 # URL 去重滑动窗口大小

    # ── 会话 ──
    max_history_turns: int = 10            # 最多保留的历史对话轮次
    session_ttl_seconds: int = 3600        # 会话缓存过期时间 (秒)

    # ── 重试 ──
    max_retries: int = 3                   # 最大重试次数
    base_delay: float = 1.0                # 指数退避基础延迟 (秒)
    max_delay: float = 30.0                # 指数退避最大延迟 (秒)

    # ── 限流 ──
    max_requests_per_minute: int = 30      # 每分钟最大请求数
    max_searches_per_minute: int = 10      # 每分钟最大搜索次数

    # ── ReAct Agent ──
    max_react_iterations: int = 6          # 最大 ReAct 迭代轮次（防死循环）
    react_recursion_limit: int = 25        # LangGraph 递归上限（模型调用 + 工具调用）
    react_temperature: float = 0.3         # ReAct 推理温度

    # ── 调试 ──
    verbose: bool = False                  # 是否输出详细日志


# ============================================================
# 单例配置
# ============================================================
_api_config: Optional[APIConfig] = None
_agent_config: Optional[AgentConfig] = None
_tdai_config: Optional[TdaiConfig] = None


def get_api_config() -> APIConfig:
    """获取 API 配置单例"""
    global _api_config
    if _api_config is None:
        _api_config = APIConfig()
    return _api_config


def get_agent_config() -> AgentConfig:
    """获取 Agent 配置单例"""
    global _agent_config
    if _agent_config is None:
        _agent_config = AgentConfig()
    return _agent_config


def get_tdai_config() -> TdaiConfig:
    """获取 TencentDB-Agent-Memory 配置单例"""
    global _tdai_config
    if _tdai_config is None:
        _tdai_config = TdaiConfig()
    return _tdai_config


def resolve_llm_connection() -> dict:
    """
    解析实际生效的 LLM 连接参数。

    优先级：LLM_PROVIDER 预设 → 显式 LLM_API_BASE/LLM_MODEL。
    当 provider 命中预设时，用预设的 base 与 model（除非显式设置了 model）。
    """
    cfg = get_api_config()
    provider = cfg.resolved_provider
    preset = get_provider_preset(provider)

    base = cfg.llm_api_base
    model = cfg.llm_model

    if preset:
        # 预设的 model 作为 provider 默认，但允许 LLM_MODEL 显式覆盖
        base = preset.get("api_base", base)
        # 若用户未显式改 model（仍是 gpt-4o-mini 默认），采用 provider 默认 model
        if cfg.llm_model == "gpt-4o-mini":
            model = preset.get("model", model)

    return {"base": base, "model": model}
