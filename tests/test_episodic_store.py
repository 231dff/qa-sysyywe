"""
智能搜索助手 — 情景记忆（EpisodicStore）单元测试

覆盖 user_id 贯穿的第三块地基：
- 按 user_id 隔离
- 追加与去重
- 关键词相关性召回 + 时间衰减
- 跨实例持久化恢复
- 路径穿越防护
- 渲染 / 清空
"""
from __future__ import annotations

import tempfile

from memory.episodic_store import EpisodicStore, _safe_user_id


class TestEpisodicStore:
    def _make(self, **kwargs):
        d = tempfile.mkdtemp()
        return d, EpisodicStore(persist_dir=d, **kwargs)

    def test_append_and_count(self):
        d, es = self._make()
        es.append("u_alice", "s1", "user", "我最近在做 Python 后端")
        es.append("u_alice", "s1", "assistant", "好的，Python 后端有什么问题？")
        assert es.count("u_alice") == 2

    def test_user_isolation(self):
        d, es = self._make()
        es.append("u_alice", "s1", "user", "我住在北京")
        assert es.count("u_bob") == 0
        assert es.recall("u_bob", "北京") == []

    def test_adjacent_dedup(self):
        """连续相同 (role, content) 不重复写入"""
        d, es = self._make()
        es.append("u_alice", "s1", "user", "重复的话")
        es.append("u_alice", "s1", "user", "重复的话")
        assert es.count("u_alice") == 1

    def test_recall_relevance(self):
        """相关性召回：命中关键词的历史应被召回"""
        d, es = self._make()
        es.append("u_alice", "s1", "user", "我最近在做 Python 后端开发")
        es.append("u_alice", "s2", "user", "今天天气不错")
        hits = es.recall("u_alice", "Python 后端", top_k=3)
        assert len(hits) >= 1
        assert "Python" in hits[0]["content"]

    def test_recall_returns_none_for_unrelated(self):
        d, es = self._make()
        es.append("u_alice", "s1", "user", "我喜欢看电影")
        hits = es.recall("u_alice", "微积分公式推导", top_k=3)
        assert hits == []

    def test_render_empty(self):
        d, es = self._make()
        assert es.render("u_nobody", "任意") == ""

    def test_persist_across_instances(self):
        d, es = self._make()
        es.append("u_alice", "s1", "user", "我用 Go 写后端")
        es2 = EpisodicStore(persist_dir=d)
        assert es2.count("u_alice") == 1
        assert "Go" in es2.recall("u_alice", "Go 后端")[0]["content"]

    def test_safe_user_id_path_traversal(self):
        d, es = self._make()
        safe = _safe_user_id("../../etc/passwd")
        assert "/" not in safe
        assert "." not in safe  # `.` 与 `/` 同属路径分隔，一并折叠防穿越
        assert safe == "______etc_passwd"

    def test_capacity_trim(self):
        d, es = self._make(max_episodes=3)
        for i in range(5):
            es.append("u_alice", "s1", "user", f"消息{i}")
        assert es.count("u_alice") == 3

    def test_clear_user(self):
        d, es = self._make()
        es.append("u_alice", "s1", "user", "内容")
        es.clear_user("u_alice")
        assert es.count("u_alice") == 0
