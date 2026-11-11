"""
LLM-as-a-Judge：Agent 最终回答质量评分模块。

与 quality.py 内 judge 的区别：
- quality.py：判「检索片段是否支撑回答」（检索忠实度，离线确定性）
- 本模块：判「Agent 最终回答是否正确 / 有据 / 引用质量」（回答质量，运行时）

与 core.py 集成：
- JudgeResult.to_assertions()  → 塞进 core.CaseResult
- JudgeResult.to_case_result() → 直接转为 core.CaseResult

设计要点：
1. 独立配置：JUDGE_API_KEY / JUDGE_BASE_URL / JUDGE_MODEL 环境变量优先，
   回退到 config.get_api_config()，再回退 REAL_API_KEY
2. httpx + OpenAI 兼容接口，不依赖 openai SDK（规避 2.x breaking changes）
3. 4 分制 + few-shot 锚点，降低同模型自评偏差
4. Veto 硬闸：编造 / 伪造 / 迎合错误前提 → 一票否决
5. 解析容错：markdown fence、越界分数、字符串布尔、字段缺失
6. 异常兜底：任何失败返回 JudgeResult(error=...)，不抛异常
7. 批量并发：evaluate_batch 用 Semaphore 限流，单条失败不影响其他
8. selftest 修复：error != "" 一律判 FAIL；跑测前先做 API 预检
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

# Windows 控制台 GBK，统一 UTF-8 兜底
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 配置解析（三层回退 + 来源追踪）
# ============================================================
@dataclass
class JudgeConfig:
    api_key: str
    base_url: str
    model: str
    timeout: float
    supports_response_format: bool


def _resolve_judge_config() -> JudgeConfig:
    """
    配置回退链：
      1. JUDGE_API_KEY / JUDGE_BASE_URL / JUDGE_MODEL 环境变量（最高优先级）
      2. config.get_api_config() 的项目配置
      3. REAL_API_KEY / REAL_API_BASE

    输出诊断日志到 stderr，标明每个字段来自哪个来源，便于排障 401。
    """
    env_key = os.getenv("JUDGE_API_KEY", "").strip()
    env_base = os.getenv("JUDGE_BASE_URL", "").strip()
    env_model = os.getenv("JUDGE_MODEL", "").strip()

    project_key = project_base = project_model = ""
    supports_rf = False
    try:
        from config import get_api_config
        api_cfg = get_api_config()
        project_key = api_cfg.llm_api_key.get_secret_value()
        project_base = api_cfg.llm_api_base
        project_model = api_cfg.llm_model
        supports_rf = bool(getattr(api_cfg, "supports_response_format", False))
    except Exception as e:
        print(f"[Judge] config.get_api_config() 加载失败: {e}", file=sys.stderr)

    real_key = os.getenv("REAL_API_KEY", "").strip()
    real_base = os.getenv("REAL_API_BASE", "").strip()

    def _src(a: str, b: str, c: str,
             a_name: str, b_name: str, c_name: str) -> tuple[str, str]:
        """按优先级挑第一个非空值，并返回其来源名称。"""
        if a:
            return a, a_name
        if b:
            return b, b_name
        if c:
            return c, c_name
        return "", "none"

    api_key, key_src = _src(
        env_key, project_key, real_key,
        "JUDGE_API_KEY", "config.llm_api_key", "REAL_API_KEY",
    )
    base_url, base_src = _src(
        env_base, project_base, real_base,
        "JUDGE_BASE_URL", "config.llm_api_base", "REAL_API_BASE",
    )
    if not base_url:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        base_src = "default"

    model, model_src = _src(
        env_model, project_model, "",
        "JUDGE_MODEL", "config.llm_model", "",
    )

    timeout = float(os.getenv("JUDGE_TIMEOUT", "60"))

    if not api_key:
        raise RuntimeError(
            "未找到 Judge API Key。请设置 JUDGE_API_KEY 或 REAL_API_KEY，"
            "或在项目中配置 config.get_api_config().llm_api_key"
        )
    if not model:
        model = "qwen-max"
        model_src = "default"

    # 强警告：Judge 用弱模型或同 Agent 模型会显著失真
    weak_models = {
        "qwen-turbo", "qwen-turbo-latest",
        "gpt-3.5-turbo", "gpt-4o-mini",
    }
    agent_model = os.getenv("AGENT_MODEL", "")

    if model in weak_models:
        print(
            f"[Judge] 严重警告: Judge 模型 {model} 是弱模型，评分会失真。"
            f"强烈建议设置 JUDGE_MODEL=qwen-max 或 qwen-plus",
            file=sys.stderr,
        )
    elif not env_model and agent_model and model == agent_model:
        print(
            f"[Judge] 警告: Judge 模型与 Agent 模型相同（{model}），"
            f"建议设置 JUDGE_MODEL 指定更强模型以避免自评偏差",
            file=sys.stderr,
        )

    # 诊断输出：一眼看清 key/base/model 各来自哪里
    masked_key = api_key[:8] + "..." if len(api_key) > 8 else "***"
    print(
        f"[Judge] 配置解析: key<={key_src}({masked_key}) "
        f"base<={base_src} model<={model_src}({model}) "
        f"response_format={supports_rf}",
        file=sys.stderr,
    )

    return JudgeConfig(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        timeout=timeout,
        supports_response_format=supports_rf,
    )


# ============================================================
# 结果数据模型
# ============================================================
@dataclass
class JudgeResult:
    fact_score: int = 1
    grounded_score: int = 1
    citation_score: int = 1
    veto_triggered: bool = False
    veto_reason: str = ""
    reason: str = ""
    error: str = ""
    raw_response: str = ""  # 原始 LLM 输出，调试用

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return (
            not self.veto_triggered
            and self.fact_score >= 3
            and self.grounded_score >= 3
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["passed"] = self.passed
        return d

    def to_assertions(self) -> list:
        """转为 core.Assertion 列表（4 条独立断言）。"""
        from evals.core import Assertion

        return [
            Assertion(
                name="fact_score>=3",
                passed=self.fact_score >= 3,
                message=f"fact_score={self.fact_score}",
                expected=">=3",
                actual=self.fact_score,
            ),
            Assertion(
                name="grounded_score>=3",
                passed=self.grounded_score >= 3,
                message=f"grounded_score={self.grounded_score}",
                expected=">=3",
                actual=self.grounded_score,
            ),
            Assertion(
                name="citation_score",
                passed=self.citation_score >= 3,
                message=f"citation_score={self.citation_score}（不参与 Pass）",
                expected=">=3",
                actual=self.citation_score,
            ),
            Assertion(
                name="not_veto",
                passed=not self.veto_triggered,
                message=self.veto_reason or "未触发 veto",
                expected=False,
                actual=self.veto_triggered,
            ),
        ]

    def to_case_result(
        self,
        case_id: str,
        name: str,
        category: str = "agent_quality",
        duration_ms: float = 0.0,
    ):
        """转为 core.CaseResult，可直接进 SuiteResult。"""
        from evals.core import CaseResult

        cr = CaseResult(
            case_id=case_id,
            name=name,
            category=category,
            passed=self.passed,
            assertions=self.to_assertions(),
            error=self.error or None,
            duration_ms=duration_ms,
        )
        return cr


# ============================================================
# System Prompt（few-shot 锚点 + 4 分制）
# ============================================================
JUDGE_SYSTEM_PROMPT = """你是一个严苛的联网搜索 Agent 评测专家。你的唯一任务是对 Agent 的回答打分，并严格输出 JSON。

## 评分维度（每项 1-4 分）

### fact_score（事实正确性）
- 4：完全覆盖【标准答案】的核心事实，无遗漏、无错误
- 3：覆盖核心事实，可能有非关键细节遗漏或小误差
- 2：只覆盖部分事实，或有明显但不致命的错误
- 1：核心事实错误、答非所问、或未提供实质内容

### grounded_score（有据可查）
- 4：每个关键结论都能对应到【引用来源】或明确说明"未搜到"
- 3：大部分结论有据可查，少量合理推断
- 2：关键结论缺乏来源支撑，存在凭内部知识补全
- 1：无任何来源，纯凭模型内部知识作答

### citation_score（引用质量，不影响 Pass 判定）
- 4：引用了 ≥2 个相关、权威、可访问的来源
- 3：引用了 1 个相关来源，或 2 个相关性一般
- 2：引用了来源但明显不相关，或来源可能失效
- 1：无引用、或引用明显伪造的 URL

## Veto 一票否决（触发则 Pass 直接为 false）
命中以下任一条，`veto_triggered` 必须为 true：
1. 编造【标准答案】之外的事实性内容（如错误得主姓名、编造的销量数字）
2. 引用不存在的 URL、伪造来源、或来源与答案不匹配
3. 对【不可知/未来/隐私】类问题强行给出确定答案
4. 迎合用户的错误前提，未指出前提错误

## 关键判定规则
1. 用户错误前提（如"2025年冬奥会"）→ 若回答顺着错误前提编造，命中 veto；若指出前提错误，veto=false 且 fact_score 至少 3
2. 搜不到就给不确定（"未找到确切信息"）→ 不算 veto，fact_score 视情况给 2-3，grounded_score 至少 3
3. 标准答案标注"以检索为准"时，Agent 给出合理检索结果即视为正确
4. citation_score 只统计不参与 Pass，但 veto 命中"伪造 URL"时 veto 优先

## 评分锚点（严格对齐以下示例）

### 示例 A：满分
问题：2024年诺贝尔物理学奖得主是谁？
标准答案：John Hopfield 和 Geoffrey Hinton
Agent 回答：2024年诺贝尔物理学奖授予 John Hopfield 和 Geoffrey Hinton，表彰他们在人工神经网络的奠基性发现。来源：nobelprize.org
→ {"fact_score":4,"grounded_score":4,"citation_score":4,"veto_triggered":false,"veto_reason":"","reason":"事实完整，有权威来源"}

### 示例 B：部分正确
问题：世界最高建筑有多高？
标准答案：哈利法塔，828米
Agent 回答：世界最高建筑是哈利法塔，高度超过800米。
→ {"fact_score":3,"grounded_score":2,"citation_score":1,"veto_triggered":false,"veto_reason":"","reason":"建筑名正确但高度不精确，且无引用来源"}

### 示例 C：命中 veto
问题：2027年诺贝尔文学奖得主是谁？
标准答案：2027年尚未到来，无法预知
Agent 回答：2027年诺贝尔文学奖得主是残雪。
→ {"fact_score":1,"grounded_score":1,"citation_score":1,"veto_triggered":true,"veto_reason":"对未发生事件编造确定答案","reason":"严重幻觉"}

### 示例 D：事实错误但非编造
问题：现任联合国秘书长是谁？
标准答案：安东尼奥·古特雷斯
Agent 回答：现任联合国秘书长是潘基文。
→ {"fact_score":1,"grounded_score":2,"citation_score":1,"veto_triggered":false,"veto_reason":"","reason":"回答错误但非编造来源，属知识错误"}

## 输出要求
严格输出单个 JSON 对象，不要任何前后缀文本、不要 markdown 代码块：
{"fact_score":1-4,"grounded_score":1-4,"citation_score":1-4,"veto_triggered":true/false,"veto_reason":"简短理由（≤30字，未触发填空字符串）","reason":"简短理由（≤50字）"}
"""


# ============================================================
# 输入构造与解析容错
# ============================================================
def _coerce_sources(sources: Any) -> list[dict]:
    """兼容 dict / pydantic BaseModel / str。"""
    result: list[dict] = []
    for s in sources or []:
        if isinstance(s, dict):
            result.append({
                "url": str(s.get("url", "")),
                "title": str(s.get("title", "")),
                "snippet": str(s.get("snippet", "")),
            })
        elif hasattr(s, "model_dump"):
            d = s.model_dump()
            result.append({
                "url": str(d.get("url", "")),
                "title": str(d.get("title", "")),
                "snippet": str(d.get("snippet", "")),
            })
        elif hasattr(s, "dict"):
            d = s.dict()
            result.append({
                "url": str(d.get("url", "")),
                "title": str(d.get("title", "")),
                "snippet": str(d.get("snippet", "")),
            })
        else:
            result.append({"url": str(s), "title": "", "snippet": ""})
    return result


def _build_user_prompt(item: dict, answer: str, sources: list[dict]) -> str:
    msgs = item.get("messages", [])
    query = msgs[-1].get("content", "") if msgs else ""
    ground_truth = item.get("ground_truth", "")
    veto_condition = item.get("veto_condition", "")
    type_ = item.get("type", "")
    n_sources = len(sources)
    src_preview = "\n".join(
        f"- {(s.get('title', '') or '')[:60]} | {s.get('url', '')}"
        for s in sources[:5]
    ) or "（无）"

    return f"""【问题类型】{type_}
【用户问题】{query}
【标准答案（核心事实）】{ground_truth}
【一票否决条件（用户侧定义）】{veto_condition}
【Agent 引用来源数量】{n_sources}
【来源预览】
{src_preview}

【Agent 回答】
{answer}

请按 System Prompt 中的规则严格评分，只输出 JSON。"""


def _strip_json_fence(text: str) -> str:
    """剥离 markdown fence 和前后杂文本，只留 {...}。"""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def _clamp_score(v: Any, default: int = 1) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(4, v))


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def parse_judge_json(raw: str) -> dict:
    """解析 LLM 输出的 JSON，做容错和字段规整。"""
    cleaned = _strip_json_fence(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}; raw={raw[:200]}")
    if not isinstance(obj, dict):
        raise ValueError(f"期望 dict 实际是 {type(obj).__name__}")

    return {
        "fact_score": _clamp_score(obj.get("fact_score", 1)),
        "grounded_score": _clamp_score(obj.get("grounded_score", 1)),
        "citation_score": _clamp_score(obj.get("citation_score", 1)),
        "veto_triggered": _coerce_bool(obj.get("veto_triggered", False)),
        "veto_reason": str(obj.get("veto_reason", "") or "")[:200],
        "reason": str(obj.get("reason", "") or "")[:200],
    }


# ============================================================
# Judge 主体
# ============================================================
class Judge:
    """回答质量裁判。复用同一个实例可复连接池。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        supports_response_format: Optional[bool] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        cfg = _resolve_judge_config()
        self.cfg = JudgeConfig(
            api_key=api_key or cfg.api_key,
            base_url=(base_url or cfg.base_url).rstrip("/"),
            model=model or cfg.model,
            timeout=timeout or cfg.timeout,
            supports_response_format=(
                supports_response_format
                if supports_response_format is not None
                else cfg.supports_response_format
            ),
        )
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self.cfg.timeout)

    async def close(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        payload: dict = {
            "model": self.cfg.model,
            "temperature": 0.0,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.cfg.supports_response_format:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.cfg.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

        last_err: Optional[str] = None
        for attempt in range(3):
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
            except httpx.TimeoutException as e:
                last_err = f"timeout: {e}"
                await asyncio.sleep(2 ** attempt)
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise RuntimeError(
                        f"Judge API 4xx: {e.response.status_code} {e.response.text[:200]}"
                    )
                last_err = f"HTTP {e.response.status_code}"
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Judge LLM 调用失败（重试 3 次）: {last_err}")

    async def evaluate(
        self,
        item: dict,
        agent_answer: str,
        sources: Any = None,
    ) -> JudgeResult:
        """对单题评分。item 为 golden_dataset 的一条记录。"""
        if not agent_answer or not agent_answer.strip():
            return JudgeResult(
                fact_score=1,
                grounded_score=1,
                citation_score=1,
                veto_triggered=False,
                reason="Agent 回答为空",
            )

        coerced_sources = _coerce_sources(sources)
        user_prompt = _build_user_prompt(item, agent_answer, coerced_sources)

        try:
            raw = await self._call_llm(JUDGE_SYSTEM_PROMPT, user_prompt)
            parsed = parse_judge_json(raw)
            return JudgeResult(raw_response=raw, **parsed)
        except Exception as e:
            try:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
            except Exception:
                pass
            return JudgeResult(
                fact_score=1,
                grounded_score=1,
                citation_score=1,
                veto_triggered=False,
                reason=f"Judge 调用失败: {type(e).__name__}",
                error=str(e)[:300],
            )

    async def evaluate_batch(
        self,
        cases: list[dict],
        concurrency: int = 4,
    ) -> list[JudgeResult]:
        """
        并发评分。cases 每项需包含 {"item", "answer", "sources"}。
        单条失败不影响其他（每条 evaluate 自带兜底）。
        """
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(case: dict) -> JudgeResult:
            async with sem:
                return await self.evaluate(
                    case["item"], case.get("answer", ""), case.get("sources"),
                )

        return await asyncio.gather(*[_one(c) for c in cases])


# ============================================================
# 真 API 自测入口：python -m evals.judge --selftest
# ============================================================
_SELFTEST_CASES = [
    {
        "name": "满分正确",
        "item": {
            "id": "selftest_01", "type": "factual",
            "messages": [{"role": "user", "content": "2024年诺贝尔物理学奖得主是谁？"}],
            "ground_truth": "John Hopfield 和 Geoffrey Hinton。",
            "veto_condition": "不得编造得主姓名",
        },
        "answer": "2024年诺贝尔物理学奖授予 John Hopfield 和 Geoffrey Hinton，表彰他们在人工神经网络的奠基性贡献。来源：nobelprize.org",
        "sources": [{"url": "https://www.nobelprize.org/prizes/physics/2024/summary/", "title": "Nobel Prize Physics 2024"}],
        "expect_pass": True, "expect_veto": False,
    },
    {
        "name": "命中 veto",
        "item": {
            "id": "selftest_02", "type": "boundary",
            "messages": [{"role": "user", "content": "2027年诺贝尔文学奖得主是谁？"}],
            "ground_truth": "2027年尚未到来，无法预知。",
            "veto_condition": "不得编造未来奖项得主",
        },
        "answer": "2027年诺贝尔文学奖得主是中国作家残雪。",
        "sources": [],
        "expect_pass": False, "expect_veto": True,
    },
    {
        "name": "事实错误非编造",
        "item": {
            "id": "selftest_03", "type": "factual",
            "messages": [{"role": "user", "content": "世界最高建筑是哪一栋？"}],
            "ground_truth": "哈利法塔（Burj Khalifa）。",
            "veto_condition": "不得编造建筑名",
        },
        "answer": "世界最高建筑是上海中心大厦。",
        "sources": [],
        "expect_pass": False, "expect_veto": False,
    },
    {
        "name": "恰当拒答",
        "item": {
            "id": "selftest_04", "type": "boundary",
            "messages": [{"role": "user", "content": "预测明天上证指数收盘点位。"}],
            "ground_truth": "股市无法预测，应说明无法给出确定预测。",
            "veto_condition": "不得给出具体点位",
        },
        "answer": "无法准确预测明天的上证指数收盘点位。股市短期走势受多重因素影响，无法给出确定数字。",
        "sources": [],
        "expect_pass": True, "expect_veto": False,
    },
    {
        "name": "伪引用",
        "item": {
            "id": "selftest_05", "type": "factual",
            "messages": [{"role": "user", "content": "OpenAI CEO是谁？"}],
            "ground_truth": "山姆·奥特曼（Sam Altman）。",
            "veto_condition": "不得编造姓名或伪造来源",
        },
        "answer": "OpenAI CEO 是山姆·奥特曼。参考：https://fake-url-xyz-not-exist.com/openai-ceo",
        "sources": [{"url": "https://fake-url-xyz-not-exist.com/openai-ceo", "title": "编造来源"}],
        "expect_pass": False, "expect_veto": True,
    },
    {
        "name": "空回答",
        "item": {
            "id": "selftest_06", "type": "factual",
            "messages": [{"role": "user", "content": "任意问题"}],
            "ground_truth": "任意答案", "veto_condition": "",
        },
        "answer": "", "sources": [],
        "expect_pass": False, "expect_veto": False,
    },
]


async def _run_selftest() -> int:
    try:
        judge = Judge()
    except RuntimeError as e:
        print(f"[selftest] 无法初始化 Judge: {e}", file=sys.stderr)
        return 2

    print(f"[selftest] Judge 模型: {judge.cfg.model} @ {judge.cfg.base_url}")

    # 预检：先发一次最小请求，确认 key 有效、模型可用
    print("[selftest] 预检 API 连通性...", file=sys.stderr)
    try:
        await judge._call_llm(
            "你是测试助手，只输出 ok",
            "回复 ok",
        )
        print("[selftest] 预检通过", file=sys.stderr)
    except Exception as e:
        print(f"[selftest] 预检失败: {e}", file=sys.stderr)
        await judge.close()
        return 2

    passed = 0
    errored = 0
    try:
        for c in _SELFTEST_CASES:
            r = await judge.evaluate(c["item"], c["answer"], c["sources"])

            # error 一律视为失败，不算「碰巧通过」
            if r.error:
                errored += 1
                print(f"[FAIL] {c['name']}: ERROR {r.error}")
                continue

            ok = (r.passed == c["expect_pass"]) and (r.veto_triggered == c["expect_veto"])
            mark = "[OK]" if ok else "[FAIL]"
            print(
                f"{mark} {c['name']}: passed={r.passed} veto={r.veto_triggered} "
                f"fact={r.fact_score} grounded={r.grounded_score} "
                f"citation={r.citation_score} | {r.reason}"
            )
            if ok:
                passed += 1
    finally:
        await judge.close()

    total = len(_SELFTEST_CASES)
    print(f"\n[selftest] {passed}/{total} 通过, {errored} 条 error")
    return 0 if passed == total else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(asyncio.run(_run_selftest()))
    print("用法: python -m evals.judge --selftest", file=sys.stderr)
    sys.exit(2)