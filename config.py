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

    llm_provider: str = Field(
        default="",
        description="LLM 提供商预设: openai | qwen | deepseek（空则用 llm_api_base 直连）",
    )
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

    @property
    def resolved_provider(self) -> str:
        return self.llm_provider.lower() if self.llm_provider else ""

    @property
    def supports_response_format(self) -> bool:
        preset = get_provider_preset(self.resolved_provider)
        if preset:
            return preset.get("supports_response_format", True)
        return True

    @property
    def supports_embedding(self) -> bool:
        preset = get_provider_preset(self.resolved_provider)
        if preset:
            return preset.get("supports_embedding", False)
        return False


# ============================================================
# TencentDB-Agent-Memory（TDAI）可选外部记忆侧车
# ============================================================
class TdaiConfig(BaseSettings):
    """TencentDB-Agent-Memory 独立 Gateway 连接配置（可选增强记忆）。"""

    tdai_enabled: bool = Field(
        default=False,
        description="是否启用 TencentDB-Agent-Memory 分层记忆侧车",
    )
    tdai_gateway_url: str = Field(
        default="http://tdai-gateway.agent-platform.svc.cluster.local:8420",
        description="TDAI Gateway 基地址（本地开发时在 .env 覆盖为 http://127.0.0.1:8420）",
    )
    tdai_api_key: str = Field(
        default="",
        description="TDAI Gateway 鉴权 Bearer Key（空=无鉴权）",
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
class AgentConfig(BaseSettings):
    """Agent 运行时参数（支持 .env 环境变量覆盖）"""

    # ── 搜索 ──
    max_search_results: int = 10
    search_depth: str = "advanced"
    search_timeout: float = 15.0
    include_domains: list[str] = []
    exclude_domains: list[str] = []

    # ── 上下文裁剪 ──
    top_k_fragments: int = 5
    max_context_tokens: int = 3000
    dedup_window: int = 50

    # ── 会话 ──
    max_history_turns: int = 10
    session_ttl_seconds: int = 3600

    # ── 重试 ──
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    # ── 限流 ──
    max_requests_per_minute: int = 30
    max_searches_per_minute: int = 10

    # ── ReAct Agent ──
    max_react_iterations: int = 6
    react_recursion_limit: int = 25
    react_temperature: float = 0.3

    # ── 时间感知 ──
    timezone: str = "Asia/Shanghai"
    inject_current_time: bool = True

    # ── 历史会话检索 ──
    history_recall_top_k: int = 5
    history_snippet_max_len: int = 500

    # ── 调试 ──
    verbose: bool = False

    # ── 会话存储（新增） ──
    session_backend: str = Field(
        default="memory",
        description="会话后端: memory（单副本）| redis（多副本共享）",
    )
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_key_prefix: str = "session:"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


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
    """
    cfg = get_api_config()
    provider = cfg.resolved_provider
    preset = get_provider_preset(provider)

    base = cfg.llm_api_base
    model = cfg.llm_model

    if preset:
        base = preset.get("api_base", base)
        if cfg.llm_model == "gpt-4o-mini":
            model = preset.get("model", model)

    return {"base": base, "model": model}