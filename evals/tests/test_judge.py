"""
judge.py 的回归测试（mock 化，不消耗 API）。

覆盖：
- 解析层：JSON 容错、markdown fence、越界分数、字符串布尔、字段缺失
- 判定层：JudgeResult.passed 矩阵、error 强制 fail、to_assertions
- 集成层：Judge.evaluate 全流程（mock httpx）
- 批量层：evaluate_batch 并发 + 错误隔离
- 转换层：to_case_result 与 core.CaseResult 互操作

真 API 自测：python -m evals.judge --selftest
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from evals.judge import (
    Judge,
    JudgeConfig,
    JudgeResult,
    _coerce_sources,
    _coerce_bool,
    _clamp_score,
    _strip_json_fence,
    parse_judge_json,
)


# ============================================================
# 解析层
# ============================================================
class TestStripJsonFence:
    def test_plain_json(self):
        assert _strip_json_fence('{"a":1}') == '{"a":1}'

    def test_markdown_fence_with_lang(self):
        assert _strip_json_fence('```json\n{"a":1}\n```') == '{"a":1}'

    def test_markdown_fence_no_lang(self):
        assert _strip_json_fence('```\n{"a":1}\n```') == '{"a":1}'

    def test_prefix_and_suffix(self):
        assert _strip_json_fence('Here:\n{"a":1}\nThanks') == '{"a":1}'

    def test_empty(self):
        assert _strip_json_fence("") == ""

    def test_no_braces(self):
        assert _strip_json_fence("no json") == "no json"


class TestClampScore:
    @pytest.mark.parametrize("inp,out", [
        (4, 4), (1, 1), (0, 1), (5, 4), (-1, 1),
        ("3", 3), ("abc", 1), (None, 1), (3.7, 3), (2.0, 2),
    ])
    def test_clamp(self, inp, out):
        assert _clamp_score(inp) == out


class TestCoerceBool:
    @pytest.mark.parametrize("inp,out", [
        (True, True), (False, False),
        ("true", True), ("TRUE", True), ("yes", True), ("y", True),
        ("false", False), ("no", False), ("", False),
        (1, True), (0, False), (None, False),
    ])
    def test_coerce(self, inp, out):
        assert _coerce_bool(inp) == out


class TestParseJudgeJSON:
    def test_valid(self):
        raw = ('{"fact_score":4,"grounded_score":3,"citation_score":2,'
               '"veto_triggered":false,"veto_reason":"","reason":"ok"}')
        d = parse_judge_json(raw)
        assert d["fact_score"] == 4
        assert d["grounded_score"] == 3
        assert d["citation_score"] == 2
        assert d["veto_triggered"] is False
        assert d["reason"] == "ok"

    def test_fenced(self):
        raw = ('```json\n{"fact_score":2,"grounded_score":2,"citation_score":1,'
               '"veto_triggered":true,"veto_reason":"x","reason":"y"}\n```')
        d = parse_judge_json(raw)
        assert d["fact_score"] == 2
        assert d["veto_triggered"] is True
        assert d["veto_reason"] == "x"

    def test_out_of_range(self):
        raw = ('{"fact_score":9,"grounded_score":-1,"citation_score":0,'
               '"veto_triggered":false,"reason":""}')
        d = parse_judge_json(raw)
        assert d["fact_score"] == 4
        assert d["grounded_score"] == 1
        assert d["citation_score"] == 1

    def test_string_bool(self):
        raw = ('{"fact_score":3,"grounded_score":3,"citation_score":3,'
               '"veto_triggered":"true","reason":""}')
        assert parse_judge_json(raw)["veto_triggered"] is True

    def test_missing_fields(self):
        d = parse_judge_json('{"veto_triggered":false}')
        assert d["fact_score"] == 1
        assert d["grounded_score"] == 1
        assert d["citation_score"] == 1

    def test_invalid_json(self):
        with pytest.raises(ValueError):
            parse_judge_json("not json at all")

    def test_not_dict(self):
        with pytest.raises(ValueError):
            parse_judge_json('["a","b"]')


# ============================================================
# 数据模型层
# ============================================================
class TestJudgeResultPassed:
    @pytest.mark.parametrize("fact,grounded,veto,expected", [
        (4, 4, False, True),
        (3, 3, False, True),
        (2, 4, False, False),
        (4, 2, False, False),
        (4, 4, True, False),
        (1, 1, True, False),
        (3, 3, True, False),
    ])
    def test_pass_matrix(self, fact, grounded, veto, expected):
        r = JudgeResult(
            fact_score=fact, grounded_score=grounded,
            citation_score=4, veto_triggered=veto,
        )
        assert r.passed is expected

    def test_citation_does_not_affect_pass(self):
        r = JudgeResult(
            fact_score=4, grounded_score=3,
            citation_score=1, veto_triggered=False,
        )
        assert r.passed is True

    def test_error_forces_fail(self):
        r = JudgeResult(
            fact_score=4, grounded_score=4,
            citation_score=4, veto_triggered=False,
            error="api error",
        )
        assert r.passed is False

    def test_to_dict_has_passed(self):
        r = JudgeResult(
            fact_score=4, grounded_score=4,
            citation_score=3, veto_triggered=False,
        )
        d = r.to_dict()
        assert d["passed"] is True
        assert d["fact_score"] == 4


class TestJudgeResultConversion:
    def test_to_assertions(self):
        r = JudgeResult(
            fact_score=4, grounded_score=3,
            citation_score=2, veto_triggered=False,
            veto_reason="", reason="ok",
        )
        assertions = r.to_assertions()
        assert len(assertions) == 4
        assert all(a.passed for a in assertions if a.name != "citation_score")
        # citation_score=2 断言应 fail（但不影响 Pass）
        citation_assert = next(a for a in assertions if a.name == "citation_score")
        assert citation_assert.passed is False

    def test_to_assertions_veto(self):
        r = JudgeResult(
            fact_score=1, grounded_score=1,
            citation_score=1, veto_triggered=True,
            veto_reason="编造",
        )
        assertions = r.to_assertions()
        veto_assert = next(a for a in assertions if a.name == "not_veto")
        assert veto_assert.passed is False

    def test_to_case_result(self):
        r = JudgeResult(
            fact_score=4, grounded_score=4,
            citation_score=3, veto_triggered=False, reason="ok",
        )
        cr = r.to_case_result("t1", "测试用例", "agent_quality")
        assert cr.case_id == "t1"
        assert cr.category == "agent_quality"
        assert cr.passed is True
        assert len(cr.assertions) == 4

    def test_to_case_result_with_error(self):
        r = JudgeResult(error="api down")
        cr = r.to_case_result("t2", "失败用例")
        assert cr.passed is False
        assert cr.error == "api down"


# ============================================================
# Sources 兼容
# ============================================================
class TestCoerceSources:
    def test_dict_list(self):
        assert _coerce_sources([{"url": "u", "title": "t"}])[0]["url"] == "u"

    def test_none(self):
        assert _coerce_sources(None) == []

    def test_string_list(self):
        r = _coerce_sources(["https://x.com"])
        assert r[0]["url"] == "https://x.com"
        assert r[0]["title"] == ""

    def test_pydantic_model(self):
        from agent.models import Source
        r = _coerce_sources([Source(url="https://x.com", title="X", snippet="s")])
        assert r[0]["url"] == "https://x.com"
        assert r[0]["title"] == "X"
        assert r[0]["snippet"] == "s"


# ============================================================
# Judge.evaluate 集成（mock httpx）
# ============================================================
_SAMPLE_ITEM = {
    "id": "t1", "type": "factual",
    "messages": [{"role": "user", "content": "测试问题"}],
    "ground_truth": "测试答案", "veto_condition": "不得编造",
}


def _mock_http_response(content: str, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.text = "error"
    return resp


def _make_judge(mock_client: AsyncMock) -> Judge:
    return Judge(
        api_key="test-key",
        base_url="http://test.local/v1",
        model="test-model",
        client=mock_client,
    )


class TestJudgeEvaluate:
    @pytest.mark.asyncio
    async def test_empty_answer_shortcut(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "", [])
        assert r.passed is False
        assert "空" in r.reason
        # 不应调 API
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_pass(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response(
            '{"fact_score":4,"grounded_score":3,"citation_score":3,'
            '"veto_triggered":false,"veto_reason":"","reason":"ok"}'
        ))
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "正确回答", [{"url": "http://x"}])
        assert r.passed is True
        assert r.fact_score == 4
        assert r.grounded_score == 3
        assert r.error == ""

    @pytest.mark.asyncio
    async def test_veto(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response(
            '{"fact_score":1,"grounded_score":1,"citation_score":1,'
            '"veto_triggered":true,"veto_reason":"编造","reason":"幻觉"}'
        ))
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "编造内容", [])
        assert r.passed is False
        assert r.veto_triggered is True
        assert r.veto_reason == "编造"

    @pytest.mark.asyncio
    async def test_markdown_fence(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response(
            '```json\n{"fact_score":3,"grounded_score":3,"citation_score":2,'
            '"veto_triggered":false,"veto_reason":"","reason":"ok"}\n```'
        ))
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "回答", [])
        assert r.passed is True
        assert r.fact_score == 3

    @pytest.mark.asyncio
    async def test_out_of_range(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response(
            '{"fact_score":10,"grounded_score":10,"citation_score":10,'
            '"veto_triggered":false,"reason":"ok"}'
        ))
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "回答", [])
        assert r.fact_score == 4
        assert r.grounded_score == 4
        assert r.citation_score == 4

    @pytest.mark.asyncio
    async def test_4xx_no_retry(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.text = "unauthorized"
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=resp,
            )
        )
        client.post = AsyncMock(return_value=resp)
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "回答", [])
        assert r.passed is False
        assert r.error != ""
        assert "401" in r.error
        # 4xx 不重试，只调一次
        assert client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retry_then_fail(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 503
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        client.post = AsyncMock(return_value=resp)
        j = _make_judge(client)

        # 把退避 sleep 改小加速测试
        with patch("evals.judge.asyncio.sleep", AsyncMock()):
            r = await j.evaluate(_SAMPLE_ITEM, "回答", [])
        assert r.passed is False
        assert r.error != ""
        assert client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_invalid_json_from_llm(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response("完全不是 JSON"))
        j = _make_judge(client)
        r = await j.evaluate(_SAMPLE_ITEM, "回答", [])
        assert r.passed is False
        assert r.error != ""


# ============================================================
# evaluate_batch
# ============================================================
class TestEvaluateBatch:
    @pytest.mark.asyncio
    async def test_batch_success(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_http_response(
            '{"fact_score":4,"grounded_score":4,"citation_score":3,'
            '"veto_triggered":false,"reason":"ok"}'
        ))
        j = _make_judge(client)
        cases = [
            {"item": _SAMPLE_ITEM, "answer": "a1", "sources": []},
            {"item": _SAMPLE_ITEM, "answer": "a2", "sources": []},
            {"item": _SAMPLE_ITEM, "answer": "a3", "sources": []},
        ]
        results = await j.evaluate_batch(cases, concurrency=2)
        assert len(results) == 3
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_batch_error_isolated(self):
        """单条失败不影响其他。"""
        client = AsyncMock(spec=httpx.AsyncClient)

        async def _post(url, **kwargs):
            # 第 2 次调用返回坏 JSON
            if _post.calls == 1:
                _post.calls += 1
                return _mock_http_response("坏数据")
            _post.calls += 1
            return _mock_http_response(
                '{"fact_score":4,"grounded_score":4,"citation_score":3,'
                '"veto_triggered":false,"reason":"ok"}'
            )
        _post.calls = 0
        client.post = AsyncMock(side_effect=_post)

        j = _make_judge(client)
        cases = [
            {"item": _SAMPLE_ITEM, "answer": "a1", "sources": []},
            {"item": _SAMPLE_ITEM, "answer": "a2", "sources": []},
            {"item": _SAMPLE_ITEM, "answer": "a3", "sources": []},
        ]
        # 串行执行以保证顺序可控
        results = await j.evaluate_batch(cases, concurrency=1)
        assert len(results) == 3
        assert sum(1 for r in results if r.error) == 1
        assert sum(1 for r in results if r.passed) == 2

    @pytest.mark.asyncio
    async def test_batch_empty(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        j = _make_judge(client)
        assert await j.evaluate_batch([], concurrency=4) == []