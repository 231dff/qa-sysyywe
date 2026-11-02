"""
智能搜索助手 — 全局配置管理 + Provider 路由层

新增 ProviderStore：
- 读写 providers.yaml（或 providers.json），维护 provider → {base_url, api_key_env, models[]}
- 内存缓存 + 文件 mtime 检测，实现热加载
- 密钥只存环境变量名（api_key_env），不落盘
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent
PROVIDERS_FILE = PROJECT_ROOT / "providers.yaml"


# ============================================================
# Provider 数据模型
# ============================================================
class ProviderModel(BaseModel):
    id: str = Field(..., description="模型 ID，如 deepseek-reasoner")
    label: str = Field(default="", description="展示名称")


class ProviderConfig(BaseModel):
    id: str = Field(..., description="Provider ID，如 deepseek-official")
    label: str = Field(default="", description="展示名称")
    base_url: str = Field(..., description="API Base URL")
    api_key_env: str = Field(..., description="环境变量名，如 DEEPSEEK_API_KEY")
    api: str = Field(default="openai-completions", description="协议类型")
    models: list[ProviderModel] = Field(default_factory=list)

    def get_api_key(self) -> str:
        """从环境变量读取密钥。缺失时返回空字符串，调用方负责报错。"""
        return os.getenv(self.api_key_env, "")

    def has_api_key(self) -> bool:
        return bool(self.get_api_key())


class ProviderStore:
    """
    线程安全的 Provider 配置存储。

    - 文件格式：providers.yaml，顶层是 providers 列表
    - 热加载：每次读取时比较文件 mtime，变化则重新加载
    - 密钥安全：只存环境变量名，不存明文
    """

    def __init__(self, path: Path = PROVIDERS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._providers: dict[str, ProviderConfig] = {}
        self._mtime: float = 0.0
        self._load()

    # ---------- 加载 ----------
    def _load(self) -> None:
        if not self._path.exists():
            self._providers = {}
            self._mtime = 0.0
            return
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[ProviderStore] 加载 {self._path} 失败: {e}")
            return

        providers = raw.get("providers", [])
        new_map: dict[str, ProviderConfig] = {}
        for item in providers:
            try:
                cfg = ProviderConfig(**item)
                new_map[cfg.id] = cfg
            except Exception as e:
                print(f"[ProviderStore] 跳过非法 provider {item}: {e}")
        self._providers = new_map
        self._mtime = self._path.stat().st_mtime

    def _ensure_fresh(self) -> None:
        """文件 mtime 变化时重新加载。"""
        if not self._path.exists():
            return
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            with self._lock:
                self._load()

    # ---------- 读 ----------
    def list_providers(self) -> list[ProviderConfig]:
        self._ensure_fresh()
        return list(self._providers.values())

    def get(self, provider_id: str) -> Optional[ProviderConfig]:
        self._ensure_fresh()
        return self._providers.get(provider_id)

    def resolve(self, provider_id: str, model_id: Optional[str] = None) -> dict:
        """
        解析出 LLM 连接参数。
        返回 { base_url, api_key, model }，失败抛 ValueError。
        """
        p = self.get(provider_id)
        if p is None:
            raise ValueError(f"未知 provider: {provider_id}")

        api_key = p.get_api_key()
        if not api_key:
            raise ValueError(
                f"Provider {provider_id} 的 api_key_env={p.api_key_env} 未设置环境变量"
            )

        chosen_model = model_id
        if not chosen_model:
            if not p.models:
                raise ValueError(f"Provider {provider_id} 未配置任何模型")
            chosen_model = p.models[0].id
        else:
            valid_ids = {m.id for m in p.models}
            if chosen_model not in valid_ids:
                raise ValueError(
                    f"模型 {chosen_model} 不属于 provider {provider_id}，"
                    f"可用: {sorted(valid_ids)}"
                )

        return {
            "base_url": p.base_url,
            "api_key": api_key,
            "model": chosen_model,
        }

    # ---------- 写 ----------
    def _save(self) -> None:
        data = {
            "providers": [p.model_dump() for p in self._providers.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self._path)
        self._mtime = self._path.stat().st_mtime

    def upsert(self, cfg: ProviderConfig) -> None:
        with self._lock:
            self._ensure_fresh()
            self._providers[cfg.id] = cfg
            self._save()

    def delete(self, provider_id: str) -> bool:
        with self._lock:
            self._ensure_fresh()
            if provider_id not in self._providers:
                return False
            del self._providers[provider_id]
            self._save()
            return True


# ============================================================
# 原有配置（保留，作为 .env 回退）
# ============================================================
class APIConfig(BaseSettings):
    llm_provider: str = Field(default="qwen")
    llm_api_key: SecretStr = Field(default=SecretStr("sk-your-api-key"))
    llm_api_base: str = Field(default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    llm_model: str = Field(default="qwen-turbo")
    llm_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    tavily_api_key: SecretStr = Field(default=SecretStr("tvly-your-api-key"))
    tavily_api_base: str = Field(default="https://api.tavily.com")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class TdaiConfig(BaseSettings):
    tdai_enabled: bool = Field(default=False)
    tdai_gateway_url: str = Field(default="http://tdai-gateway.agent-platform.svc.cluster.local:8420")
    tdai_api_key: str = Field(default="")
    tdai_timeout: float = Field(default=30.0, gt=0.0)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class AgentConfig(BaseSettings):
    max_search_results: int = 10
    search_depth: str = "advanced"
    search_timeout: float = 15.0
    include_domains: list[str] = []
    exclude_domains: list[str] = []
    top_k_fragments: int = 5
    max_context_tokens: int = 3000
    dedup_window: int = 50
    max_history_turns: int = 10
    session_ttl_seconds: int = 604800
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_requests_per_minute: int = 30
    max_searches_per_minute: int = 10
    max_react_iterations: int = 6
    react_recursion_limit: int = 40
    react_temperature: float = 0.3
    timezone: str = "Asia/Shanghai"
    inject_current_time: bool = True
    history_recall_top_k: int = 5
    history_snippet_max_len: int = 500
    verbose: bool = False
    session_backend: str = Field(default="memory")
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_key_prefix: str = "session:"
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


# ============================================================
# 单例
# ============================================================
_api_config: Optional[APIConfig] = None
_agent_config: Optional[AgentConfig] = None
_tdai_config: Optional[TdaiConfig] = None
_provider_store: Optional[ProviderStore] = None


def get_api_config() -> APIConfig:
    global _api_config
    if _api_config is None:
        _api_config = APIConfig()
    return _api_config


def get_agent_config() -> AgentConfig:
    global _agent_config
    if _agent_config is None:
        _agent_config = AgentConfig()
    return _agent_config


def get_tdai_config() -> TdaiConfig:
    global _tdai_config
    if _tdai_config is None:
        _tdai_config = TdaiConfig()
    return _tdai_config


def get_provider_store() -> ProviderStore:
    global _provider_store
    if _provider_store is None:
        _provider_store = ProviderStore()
    return _provider_store


def resolve_llm_connection(provider_id: Optional[str] = None, model_id: Optional[str] = None) -> dict:
    """
    统一入口：优先从 ProviderStore 解析；不存在时回退到 .env 默认配置。
    返回 { base, model, api_key }。
    """
    store = get_provider_store()

    if provider_id:
        resolved = store.resolve(provider_id, model_id)
        return {
            "base": resolved["base_url"],
            "model": resolved["model"],
            "api_key": resolved["api_key"],
        }

    cfg = get_api_config()
    return {
        "base": cfg.llm_api_base,
        "model": cfg.llm_model,
        "api_key": cfg.llm_api_key.get_secret_value(),
    }