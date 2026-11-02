"""
智能搜索助手 — TencentDB-Agent-Memory（TDAI）侧车适配层

★ 超时契约：共享客户端模式下，client.get/post 必须显式传 timeout=
"""
from __future__ import annotations

from typing import Optional

import httpx

from config import get_tdai_config
from utils.http_client import get_http_client


class TdaiAdapter:
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

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(self, path: str, payload: dict) -> Optional[dict]:
        try:
            async with get_http_client(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,          # ★ 显式请求级超时
                )
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def health(self) -> bool:
        health_timeout = min(self.timeout, 5.0)
        try:
            async with get_http_client(timeout=health_timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/health",
                    headers=self._headers(),
                    timeout=health_timeout,        # ★ 显式请求级超时
                )
                resp.raise_for_status()
                return resp.json().get("status") in ("ok", "degraded")
        except (httpx.HTTPError, ValueError):
            return False

    async def recall(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
        query: str,
    ) -> str:
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

    async def capture(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
        user_content: str,
        assistant_content: str,
    ) -> dict:
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

    async def end_session(
        self,
        user_id: Optional[str],
        session_id: Optional[str],
    ) -> bool:
        if not user_id:
            return False
        data = await self._post(
            "/session/end",
            {"session_key": session_id or "", "user_id": user_id},
        )
        return bool(data and data.get("flushed", False))

    async def search_memories(
        self,
        query: str,
        limit: int = 10,
        scene: Optional[str] = None,
    ) -> dict:
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


_adapter: Optional[TdaiAdapter] = None


def get_tdai_adapter() -> TdaiAdapter:
    global _adapter
    if _adapter is None:
        _adapter = TdaiAdapter()
    return _adapter