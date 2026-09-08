"""
智能搜索助手 — 语义记忆（FactStore）单元测试

覆盖 user_id 贯穿的第二块地基：
- 按 user_id 隔离
- upsert 合并与置信度择优
- 跨实例持久化恢复
- 路径穿越防护
- 渲染 / 删除 / 清空
"""
from __future__ import annotations

import tempfile

from memory.fact_store import FactStore, _safe_user_id


class TestFactStore:
    def _make(self):
        d = tempfile.mkdtemp()
        return d, FactStore(persist_dir=d)

    def test_upsert_and_get(self):
        d, fs = self._make()
        fs.upsert("u_alice", [{"key": "名字", "value": "小明", "confidence": 0.9}])
        facts = fs.get_facts("u_alice")
        assert len(facts) == 1
        assert facts[0]["key"] == "名字"
        assert facts[0]["value"] == "小明"

    def test_user_isolation(self):
        d, fs = self._make()
        fs.upsert("u_alice", [{"key": "名字", "value": "小明"}])
        assert fs.get_facts("u_bob") == []
        assert fs.count("u_bob") == 0

    def test_confidence_priority(self):
        """高置信度事实不应被低置信度覆盖"""
        d, fs = self._make()
        fs.upsert("u_alice", [{"key": "城市", "value": "北京", "confidence": 0.9}])
        fs.upsert("u_alice", [{"key": "城市", "value": "上海", "confidence": 0.4}])
        facts = fs.get_facts("u_alice")
        assert facts[0]["value"] == "北京"

    def test_confidence_override(self):
        """更高置信度应覆盖旧值"""
        d, fs = self._make()
        fs.upsert("u_alice", [{"key": "城市", "value": "北京", "confidence": 0.5}])
        fs.upsert("u_alice", [{"key": "城市", "value": "上海", "confidence": 0.8}])
        facts = fs.get_facts("u_alice")
        assert facts[0]["value"] == "上海"

    def test_persist_across_instances(self):
        d, fs = self._make()
        fs.upsert("u_alice", [{"key": "名字", "value": "小明"}])

        fs2 = FactStore(persist_dir=d)
        assert fs2.get_facts("u_alice")[0]["value"] == "小明"

    def test_safe_user_id_path_traversal(self):
        d, fs = self._make()
        safe = _safe_user_id("../../etc/passwd")
        assert "/" not in safe
        assert "." not in safe  # `.` 与 `/` 同属路径分隔，一并折叠防穿越
        assert safe == "______etc_passwd"

    def test_render_sorted_by_confidence(self):
        d, fs = self._make()
        fs.upsert("u_alice", [
            {"key": "低", "value": "a", "confidence": 0.3},
            {"key": "高", "value": "b", "confidence": 0.9},
        ])
        text = fs.render("u_alice")
        assert text.index("高") < text.index("低")

    def test_render_empty(self):
        d, fs = self._make()
        assert fs.render("u_nobody") == ""

    def test_delete_and_clear(self):
        d, fs = self._make()
        fs.upsert("u_alice", [
            {"key": "a", "value": "1"},
            {"key": "b", "value": "2"},
        ])
        assert fs.delete("u_alice", "a") is True
        assert fs.delete("u_alice", "a") is False
        fs.clear_user("u_alice")
        assert fs.count("u_alice") == 0
