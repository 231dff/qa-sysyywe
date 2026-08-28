"""
智能搜索助手 — Agent 主类（薄封装）

SearchAgent 对外保持原有接口不变，内部委托 ReactAgent（基于 LangGraph
create_react_agent 的自主决策循环）。会话历史由本类维护与裁剪。

保留方法：
- run(user_query, session_id) → GeneratedAnswer
- run_stream(user_query, session_id) → AsyncGenerator（Streamlit 用）
- get_history(session_id) / clear_session(session_id)
"""
from __future__ import annotations

from typing import Optional

from agent.models import GeneratedAnswer
from agent.react import ReactAgent
from config import get_agent_config
from utils.guardrails import check_input_guardrails
from utils.helpers import URLDeduplicator, generate_session_id


class SearchAgent:
    """
    智能搜索 Agent（ReAct 范式）

    用法:
        agent = SearchAgent()
        answer = await agent.run("今天有什么重大新闻？")
        print(answer.answer)
    """

    def __init__(self, verbose: bool = False):
        if verbose:
            get_agent_config().verbose = True

        self.deduplicator = URLDeduplicator(
            window_size=get_agent_config().dedup_window
        )
        self._sessions: dict[str, list[dict[str, str]]] = {}
        self._react = ReactAgent(verbose=verbose)

    async def run(
        self,
        user_query: str,
        session_id: Optional[str] = None,
    ) -> GeneratedAnswer:
        """执行完整的 ReAct 问答流水线"""
        agent_cfg = get_agent_config()

        # 输入护栏：提示注入检测
        guard = check_input_guardrails(user_query)
        if guard.blocked:
            return GeneratedAnswer(
                query=user_query,
                answer="抱歉，该请求可能包含不安全的指令，已拒绝处理。",
                sources=[],
                confidence=0.0,
                is_fallback=True,
                tokens_used=0,
                latency_ms=0.0,
            )

        # 会话管理
        if session_id is None:
            session_id = generate_session_id()
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        history = self._sessions[session_id]

        if agent_cfg.verbose:
            print(f"\n{'='*60}")
            print(f"[Agent] session={session_id}, query={user_query[:80]}...")
            print(f"[Agent] 历史轮次: {len(history)//2}")

        # 委托 ReactAgent 执行单轮
        final_answer = await self._react.run(user_query, history)

        # 更新对话历史
        history.append({"role": "user", "content": user_query})
        history.append({"role": "assistant", "content": final_answer.answer})

        # 裁剪历史
        max_turns = agent_cfg.max_history_turns
        if len(history) > max_turns * 2:
            self._sessions[session_id] = history[-(max_turns * 2):]
        else:
            self._sessions[session_id] = history

        if agent_cfg.verbose:
            status = "⚠️ 降级" if final_answer.is_fallback else "✅ 正常"
            print(f"[Agent] 完成 {status} | 置信度:{final_answer.confidence:.0%} "
                  f"| 耗时:{final_answer.latency_ms:.0f}ms "
                  f"| Token:{final_answer.tokens_used}")
            print(f"{'='*60}\n")

        return final_answer

    async def run_stream(self, user_query: str, session_id: str = "default"):
        """流式运行（用于 Streamlit 前端），yield 分步输出状态"""
        yield {"type": "status", "message": "🔄 正在处理..."}
        result = await self.run(user_query, session_id)
        yield {"type": "status", "message": "✅ 完成"}
        yield {"type": "answer", "data": result}

    def get_history(self, session_id: str) -> list[dict[str, str]]:
        """获取会话历史"""
        return self._sessions.get(session_id, [])

    def clear_session(self, session_id: str) -> None:
        """清除会话"""
        self._sessions.pop(session_id, None)
        self.deduplicator.clear()
