"""
智能搜索助手 — TencentDB-Agent-Memory（TDAI）侧车适配层测试

覆盖 TdaiAdapter 的每个端点与降级语义：
- recall / capture / end_session / search_memories / search_conversations / health
- Gateway 异常 / 超时 / 非法 JSON → 安全默认值（不抛异常）
- user_id 为空 → 短路返回空
- 鉴权 Bearer 头
策略: mock httpx.AsyncClient 隔离所有外部 HTTP 调用（无需联网）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from memory.tdai_adapter import TdaiAdapter, get_tdai_adapter


# ============================================================
# 测试工具函数
# ============================================================
def make_mock_response(json_data: dict, status_code: int = 200):
    """构造 mock httpx.Response"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def make_adapter(api_key: str = ""):
    """构造一个不依赖 .env 的 TdaiAdapter，base_url 指向本地假地址。"""
    return TdaiAdapter(gateway_url="http://127.0.0.1:8420", api_key=api_key, timeout=5.0)


def patch_client(adapter, post_side_effect=None, get_side_effect=None):
    """
    用假 httpx.AsyncClient 替换 get_http_client 内部新建的客户端。

    Returns:
        (mock_client, post_calls, get_calls) 供断言请求体/头。
    """
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=post_side_effect)
    mock_client.get = AsyncMock(side_effect=get_side_effect)

    p = patch("httpx.AsyncClient", return_value=mock_client)
    return p, mock_client


# ============================================================
# recall
# ============================================================
class TestTdaiRecall:
    @pytest.mark.asyncio
    async def test_recall_returns_context(self):
        """正常召回返回可注入文本"""
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"context": "## 关于该用户的长期记忆\n- 名字: 小明"})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            ctx = await adapter.recall("u_alice", "s1", "帮我看看 Python 后端")

        assert "长期记忆" in ctx
        # 验证请求路径与 payload
        call = client.post.call_args
        assert call.args[0] == "http://127.0.0.1:8420/recall"
        assert call.kwargs["json"] == {
            "query": "帮我看看 Python 后端",
            "session_key": "s1",
            "user_id": "u_alice",
        }

    @pytest.mark.asyncio
    async def test_recall_empty_user_id_shortcircuits(self):
        """user_id 为空直接返回空，不发 HTTP 请求"""
        adapter = make_adapter()
        p, client = patch_client(adapter)
        with p:
            ctx = await adapter.recall(None, "s1", "任意")
        assert ctx == ""
        client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recall_gateway_error_returns_empty(self):
        """Gateway 抛异常 → 返回空字符串，不向上抛"""
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        p, _ = patch_client(adapter, post_side_effect=mock_post)
        with p:
            ctx = await adapter.recall("u_alice", "s1", "任意")
        assert ctx == ""

    @pytest.mark.asyncio
    async def test_recall_missing_context_returns_empty(self):
        """响应缺 context 字段 → 空字符串"""
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"strategy": "none"})

        p, _ = patch_client(adapter, post_side_effect=mock_post)
        with p:
            ctx = await adapter.recall("u_alice", "s1", "任意")
        assert ctx == ""


# ============================================================
# capture
# ============================================================
class TestTdaiCapture:
    @pytest.mark.asyncio
    async def test_capture_returns_recorded_counts(self):
        """正常捕获返回落盘计数"""
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"l0_recorded": 2, "scheduler_notified": True})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            result = await adapter.capture("u_alice", "s1", "我叫小明", "好的")

        assert result == {"l0_recorded": 2, "scheduler_notified": True}
        call = client.post.call_args
        assert call.args[0] == "http://127.0.0.1:8420/capture"
        assert call.kwargs["json"]["user_content"] == "我叫小明"
        assert call.kwargs["json"]["assistant_content"] == "好的"

    @pytest.mark.asyncio
    async def test_capture_empty_user_id_noop(self):
        adapter = make_adapter()
        p, client = patch_client(adapter)
        with p:
            result = await adapter.capture(None, "s1", "a", "b")
        assert result == {}
        client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capture_error_returns_empty_dict(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            raise httpx.TimeoutException("timeout")

        p, _ = patch_client(adapter, post_side_effect=mock_post)
        with p:
            result = await adapter.capture("u_alice", "s1", "a", "b")
        assert result == {}


# ============================================================
# end_session
# ============================================================
class TestTdaiEndSession:
    @pytest.mark.asyncio
    async def test_end_session_flushed(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"flushed": True})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            flushed = await adapter.end_session("u_alice", "s1")
        assert flushed is True
        call = client.post.call_args
        assert call.args[0] == "http://127.0.0.1:8420/session/end"

    @pytest.mark.asyncio
    async def test_end_session_error_returns_false(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            raise httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())

        p, _ = patch_client(adapter, post_side_effect=mock_post)
        with p:
            flushed = await adapter.end_session("u_alice", "s1")
        assert flushed is False


# ============================================================
# 搜索
# ============================================================
class TestTdaiSearch:
    @pytest.mark.asyncio
    async def test_search_memories(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"results": "l1...", "total": 3, "strategy": "vector"})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            result = await adapter.search_memories("Python", limit=5, scene="work")
        assert result == {"results": "l1...", "total": 3, "strategy": "vector"}
        call = client.post.call_args
        assert call.args[0] == "http://127.0.0.1:8420/search/memories"
        assert call.kwargs["json"]["limit"] == 5
        assert call.kwargs["json"]["scene"] == "work"

    @pytest.mark.asyncio
    async def test_search_memories_error_returns_empty_dict(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({}, status_code=500)

        p, _ = patch_client(adapter, post_side_effect=mock_post)
        with p:
            result = await adapter.search_memories("Python")
        assert result == {}

    @pytest.mark.asyncio
    async def test_search_conversations(self):
        adapter = make_adapter()

        async def mock_post(url, **kwargs):
            return make_mock_response({"results": "l0...", "total": 7})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            result = await adapter.search_conversations("Python", limit=10, session_id="s1")
        assert result == {"results": "l0...", "total": 7}
        call = client.post.call_args
        assert call.args[0] == "http://127.0.0.1:8420/search/conversations"
        assert call.kwargs["json"]["session_key"] == "s1"


# ============================================================
# health + 鉴权
# ============================================================
class TestTdaiHealthAndAuth:
    @pytest.mark.asyncio
    async def test_health_ok(self):
        adapter = make_adapter()

        async def mock_get(url, **kwargs):
            return make_mock_response({"status": "ok"})

        p, client = patch_client(adapter, get_side_effect=mock_get)
        with p:
            healthy = await adapter.health()
        assert healthy is True

    @pytest.mark.asyncio
    async def test_health_down_returns_false(self):
        adapter = make_adapter()

        async def mock_get(url, **kwargs):
            raise httpx.ConnectError("down")

        p, _ = patch_client(adapter, get_side_effect=mock_get)
        with p:
            healthy = await adapter.health()
        assert healthy is False

    @pytest.mark.asyncio
    async def test_bearer_auth_header_when_api_key_set(self):
        """设置 api_key 时，POST 应附带 Authorization Bearer 头"""
        adapter = make_adapter(api_key="secret-123")

        async def mock_post(url, **kwargs):
            return make_mock_response({"context": ""})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            await adapter.recall("u_alice", "s1", "任意")
        _, kwargs = client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret-123"

    @pytest.mark.asyncio
    async def test_no_auth_header_when_api_key_empty(self):
        """未设置 api_key 时，不附加 Authorization 头"""
        adapter = make_adapter(api_key="")

        async def mock_post(url, **kwargs):
            return make_mock_response({"context": ""})

        p, client = patch_client(adapter, post_side_effect=mock_post)
        with p:
            await adapter.recall("u_alice", "s1", "任意")
        _, kwargs = client.post.call_args
        assert "Authorization" not in kwargs["headers"]


# ============================================================
# 单例
# ============================================================
class TestTdaiSingleton:
    def test_get_tdai_adapter_returns_same_instance(self):
        a1 = get_tdai_adapter()
        a2 = get_tdai_adapter()
        assert a1 is a2
