"""
共享 HTTP 客户端 — 连接池复用
消除每次 API 调用的 TCP/TLS 握手开销 (~200-500ms × 每次调用)

★ 超时契约（对齐 DeepSeek Harness「超时是请求属性」原则）：
- 共享客户端（生产模式）的 client 级 timeout 仅作为连接级兜底，
  不承载业务语义。业务超时必须在每次 client.get/post(...) 上显式传 timeout=，
  否则会被共享客户端的默认值静默覆盖（曾导致搜索 API 卡 60s、首 token 8s 触发降级）。
- 无共享客户端（测试/开发）时，timeout 参数作为临时客户端的默认值。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import httpx

_shared_client: Optional[httpx.AsyncClient] = None


def set_shared_client(client: httpx.AsyncClient) -> None:
    """注入全局共享客户端 (在 server.py startup 时调用)"""
    global _shared_client
    _shared_client = client


def reset_shared_client() -> None:
    """关闭并重置共享客户端 (用于测试清理)"""
    global _shared_client
    if _shared_client is not None:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_shared_client.aclose())
        except RuntimeError:
            pass
    _shared_client = None


@asynccontextmanager
async def get_http_client(timeout: float = 30.0):
    """
    获取 HTTP 客户端。

    ★ 共享客户端模式下 timeout 参数不生效：
      调用方必须在每次请求上显式传 timeout=，例如：

          async with get_http_client(timeout=15.0) as client:
              resp = await client.post(url, json=payload, timeout=15.0)

      不传 timeout= 的请求会使用共享客户端的连接级默认值（server.py 启动时设为 60s）。
    """
    if _shared_client is not None:
        yield _shared_client
    else:
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client