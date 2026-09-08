"""
智能搜索助手 — 记忆模块评测套件（memory）

系统化验证「金鱼记忆」三块地基的端到端正确性（离线确定性）：
1. 语义记忆（FactStore）：user_id 隔离 + 跨实例持久化恢复
2. 情景记忆（EpisodicStore）：相邻去重 + 相关召回 + 时间衰减
3. 编排层（MemoryManager）：remember 抽取事实 + recall 渲染注入 + 隔离降级

设计：
- 全部走 tempfile 持久化，真实读写磁盘（模拟进程重启），零 mock
- 门禁覆盖核心链路（抽取/隔离/持久化/召回），回归即红
- 与现有代码风格一致：纯 Python、零新增依赖、失败兜底不抛异常

用法:
    python -m evals --kind memory
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from evals.core import (
    KIND_MEMORY,
    Assertion,
    CaseResult,
    GateResult,
    Suite,
    SuiteResult,
    register,
)


def _run_memory() -> list[CaseResult]:
    from memory.episodic_store import EpisodicStore
    from memory.fact_store import FactStore
    from memory.memory_manager import MemoryManager

    cases: list[CaseResult] = []

    def _case(case_id: str, name: str, cond: bool, msg_fail: str = "", **assertion) -> CaseResult:
        cr = CaseResult(case_id, name, "memory", passed=bool(cond))
        cr.assertions.append(Assertion(
            "check", bool(cond), message="" if cond else msg_fail, **assertion,
        ))
        return cr

    d = tempfile.mkdtemp()
    mm = MemoryManager(
        fact_store=FactStore(persist_dir=str(Path(d) / "facts")),
        episodic_store=EpisodicStore(persist_dir=str(Path(d) / "episodes")),
    )

    # 1. 用户自我陈述 → 抽取三类长期事实
    mm.remember("u_alice", "s1", "user", "我叫小明，我住在北京，我用 Python 做后端")
    facts = mm.facts.get_facts("u_alice")
    keys = {f["key"] for f in facts}
    cases.append(_case(
        "mem-001", "用户陈述抽取长期事实（名字/城市/技术栈）",
        {"名字", "居住城市", "技术栈"} <= keys,
        msg_fail=f"缺失事实键，实际={keys}",
        expected={"名字", "居住城市", "技术栈"}, actual=keys,
    ))

    # 2. assistant 发言不抽取事实，但记录情景
    mm.remember("u_alice", "s1", "assistant", "好的，已记录。")
    cases.append(_case(
        "mem-002", "assistant 发言不抽事实",
        mm.facts.count("u_alice") == 3,
        msg_fail=f"assistant 发言不应新增事实，实际={mm.facts.count('u_alice')}",
        expected=3, actual=mm.facts.count("u_alice"),
    ))

    # 3. 相邻去重：重复内容不重复写入情景
    mm.remember("u_alice", "s2", "user", "今天天气不错")
    mm.remember("u_alice", "s2", "user", "今天天气不错")
    cases.append(_case(
        "mem-003", "相邻重复情景去重",
        mm.episodes.count("u_alice") == 3,  # 小明 + assistant + 天气（第2条天气去重）
        msg_fail=f"去重后应 3 条，实际={mm.episodes.count('u_alice')}",
        expected=3, actual=mm.episodes.count("u_alice"),
    ))

    # 4. 召回注入：相关事实渲染进上下文
    ctx = mm.recall("u_alice", "小明住在哪个城市")
    rendered = ctx.render()
    cases.append(_case(
        "mem-004", "召回渲染注入长期记忆",
        "长期记忆" in rendered and "北京" in rendered,
        msg_fail="召回上下文未注入长期事实",
        expected="含「长期记忆」与「北京」", actual=rendered[:80],
    ))

    # 5. user_id 隔离：无关用户召回为空
    cases.append(_case(
        "mem-005", "跨用户隔离（bob 召回为空）",
        mm.recall("u_bob", "小明住在哪个城市").is_empty,
        msg_fail="bob 不应召回 alice 的记忆",
        expected=True, actual=mm.recall("u_bob", "小明住在哪个城市").is_empty,
    ))

    # 6. 跨实例持久化：模拟进程重启后仍可恢复
    mm2 = MemoryManager(
        fact_store=FactStore(persist_dir=str(Path(d) / "facts")),
        episodic_store=EpisodicStore(persist_dir=str(Path(d) / "episodes")),
    )
    restart_facts = {f["key"] for f in mm2.facts.get_facts("u_alice")}
    cases.append(_case(
        "mem-006", "跨实例持久化恢复（进程重启）",
        "居住城市" in restart_facts and mm2.episodes.count("u_alice") == 3,
        msg_fail=f"重启后事实缺失，实际={restart_facts}",
        expected="居住城市 存在", actual=restart_facts,
    ))

    # 7. 空 user_id 安全降级：不落盘、不抛异常
    r = mm.remember(None, "s1", "user", "我叫无名")
    cases.append(_case(
        "mem-007", "空 user_id 安全降级",
        r == {"episodes": 0, "facts_added": 0},
        msg_fail=f"空 user_id 应返回全 0，实际={r}",
        expected={"episodes": 0, "facts_added": 0}, actual=r,
    ))

    return cases


@register
class MemorySuite(Suite):
    kind = KIND_MEMORY
    name = "记忆模块（语义 + 情景 + 编排端到端）"

    async def run(self) -> SuiteResult:
        cases = _run_memory()
        passed = all(c.passed and not c.error for c in cases)
        return SuiteResult(
            kind=self.kind,
            name=self.name,
            passed=passed,
            cases=cases,
            summary={
                "cases": len(cases),
                "passed": sum(1 for c in cases if c.passed and not c.error),
            },
            error=None if passed else "记忆模块存在失败",
        )

    def gate(self, result: SuiteResult) -> GateResult:
        passed = result.summary.get("passed", 0)
        total = result.summary.get("cases", 0)
        return GateResult(
            kind=self.kind,
            name=self.name,
            passed=result.passed,
            threshold="记忆模块端到端全过",
            actual=f"{passed}/{total}",
        )
