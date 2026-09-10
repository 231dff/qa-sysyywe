"""
智能搜索助手 — TencentDB-Agent-Memory（TDAI）侧车适配层

把 TencentDB-Agent-Memory 的独立 Gateway（Node 侧，默认 127.0.0.1:8420）
封装成与自研 MemoryManager 同构的异步原语。Gateway 暴露纯 HTTP 端点：

    GET  /health                 — 健康检查
    POST /recall                 — 记忆召回（prefetch，返回可注入 context）
    POST /capture                — 对话捕获（一轮 user/assistant 落盘 + 调度抽取）
    POST /search/memories        — L1 记忆搜索
    POST /search/conversations   — L0 对话搜索
    POST /session/end            — 会话结束 flush

设计要点（延续项目降级语义）：
- 复用 utils.http_client.get_http_client 的全局连接池，消除握手开销。
- 任何 HTTP 异常 / 超时 / 非 2xx / 响应缺失，一律捕获后返回安全默认值
  （空字符串 / 空列表 / False），绝不向上抛异常，上层可无条件调用。
- 启用鉴权时（tdai_api_key 非空）自动附加 Authorization: Bearer 头。
"""
from __future__ import annotations

from typing import Optional

import httpx

from config import get_tdai_config
from utils.http_client import get_http_client


class TdaiAdapter:
    """
    TencentDB-Agent-Memory Gateway 的异步 HTTP 客户端封装。

    用法:
        adapter = TdaiAdapter()
        ctx = await adapter.recall("u_alice", "s1", "帮我看看 Python 后端")
        await adapter.capture("u_alice", "s1", "我用 Python 做后端", "好的")

    所有方法均安全降级：Gateway 不可用时返回空，不阻断主链路。
    """

    def __init__(
        self,
        gateway_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        cfg = get_tdai_config()
        self.base_url = (gateway_url or cfg.tdai_gateway_url).rstrip("/")
        self.api_key = api_key if api_key is not None else cfg.tdai_api_key
        self.timeout = timeout or cfg.tdai_timeout

    # ── 底层请求 ──
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(self, path: str, payload: dict) -> Optional[dict]:
        """
        POST 到 Gateway 的通用封装。

        Returns:
            解析后的 JSON dict；任何失败（超时/连接/非 2xx/非法 JSON）返回 None。
        """
        try:
            async with get_http_client(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError):
            # httpx.HTTPError 覆盖 TimeoutException / ConnectError / HTTPStatusError；
            # ValueError 覆盖响应体非 JSON。全部静默降级。
            return None

    # ── 健康检查 ──
    async def health(self) -> bool:
        """Gateway 是否健康。任何异常返回 False。"""
        try:
            async with get_http_client(timeout=min(self.timeout, 5.0)) as client:
                resp = await client.get(
                    f"{self.base_url}/health",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json().get("status") in ("ok", "degraded")
        except (httpx.HTTPError, ValueError):
            return False

    # ── 记忆召回 ──
    async def recall(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
        query: str,
    ) -> str:
        """
        召回该用户与当前 query 相关的记忆，返回可直接注入 LLM 的文本块。

        Returns:
            注入用文本；Gateway 不可用 / 无记忆时返回空字符串。
        """
        if not user_id:
            return ""

        data = await self._post(
            "/recall",
            {
                "query": query,
                "session_key": session_id or "",
                "user_id": user_id,
            },
        )
        if not data:
            return ""
        return str(data.get("context", "") or "")

    # ── 对话捕获 ──
    async def capture(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
        user_content: str,
        assistant_content: str,
    ) -> dict:
        """
        捕获一轮对话，交给 Gateway 落盘 L0 并调度 L1/L2/L3 抽取。

        Returns:
            {"l0_recorded": int, "scheduler_notified": bool}；失败时返回空 dict。
        """
        if not user_id:
            return {}

        data = await self._post(
            "/capture",
            {
                "user_content": user_content or "",
                "assistant_content": assistant_content or "",
                "session_key": session_id or "",
                "user_id": user_id,
            },
        )
        if not data:
            return {}
        return {
            "l0_recorded": int(data.get("l0_recorded", 0) or 0),
            "scheduler_notified": bool(data.get("scheduler_notified", False)),
        }

    # ── 会话结束 ──
    async def end_session(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
    ) -> bool:
        """结束会话并 flush 该 session 的待处理抽取。返回是否成功 flush。"""
        if not user_id:
            return False

        data = await self._post(
            "/session/end",
            {"session_key": session_id or "", "user_id": user_id},
        )
        return bool(data and data.get("flushed", False))

    # ── 搜索 ──
    async def search_memories(
        self,
        query: str,
        limit: int = 10,
        scene: Optional[str] = None,
    ) -> dict:
        """
        搜索 L1 记忆（原子事实/场景）。供观测与调试。

        Returns:
            {"results": str, "total": int, "strategy": str}；失败时返回空 dict。
        """
        payload: dict = {"query": query, "limit": limit}
        if scene:
            payload["scene"] = scene
        data = await self._post("/search/memories", payload)
        if not data:
            return {}
        return {
            "results": str(data.get("results", "") or ""),
            "total": int(data.get("total", 0) or 0),
            "strategy": str(data.get("strategy", "") or ""),
        }

    async def search_conversations(
        self,
        query: str,
        limit: int = 10,
        session_id: Optional[str] = None,
    ) -> dict:
        """
        搜索 L0 原始对话。供观测与调试。

        Returns:
            {"results": str, "total": int}；失败时返回空 dict。
        """
        payload: dict = {"query": query, "limit": limit}
        if session_id:
            payload["session_key"] = session_id
        data = await self._post("/search/conversations", payload)
        if not data:
            return {}
        return {
            "results": str(data.get("results", "") or ""),
            "total": int(data.get("total", 0) or 0),
        }


# 全局单例
_adapter: Optional[TdaiAdapter] = None


def get_tdai_adapter() -> TdaiAdapter:
    global _adapter
    if _adapter is None:
        _adapter = TdaiAdapter()
    return _adapter
