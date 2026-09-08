"""
智能搜索助手 — 用户存储单元测试

覆盖 user_id 贯穿的第一块地基：
- 会话按 user_id 隔离
- 跨实例持久化恢复（模拟进程/容器重启）
- 路径穿越防护
- 删除 / 清空
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from memory.user_store import UserStore, _safe_user_id


class TestUserStore:
    def _make(self):
        d = tempfile.mkdtemp()
        return d, UserStore(persist_dir=d)

    def test_set_and_get_history(self):
        d, us = self._make()
        us.set_history("u_alice", "s1", [{"role": "user", "content": "我叫小明"}])
        assert us.get_history("u_alice", "s1") == [{"role": "user", "content": "我叫小明"}]

    def test_user_isolation(self):
        d, us = self._make()
        us.set_history("u_alice", "s1", [{"role": "user", "content": "a"}])
        assert us.get_history("u_bob", "s1") == []
        assert us.get_history("u_alice", "nonexistent") == []

    def test_persist_across_instances(self):
        """模拟进程重启：新实例从磁盘恢复历史"""
        d, us = self._make()
        us.set_history("u_alice", "s1", [{"role": "user", "content": "hi"}])

        us2 = UserStore(persist_dir=d)
        assert us2.get_history("u_alice", "s1") == [{"role": "user", "content": "hi"}]
        assert us2.exists("u_alice") is True

    def test_safe_user_id_path_traversal(self):
        d, us = self._make()
        safe = _safe_user_id("../../etc/passwd")
        assert "/" not in safe
        assert "." not in safe  # `.` 与 `/` 同属路径分隔，一并折叠防穿越
        assert safe == "______etc_passwd"

    def test_safe_user_id_normal(self):
        assert _safe_user_id("u_alice_123") == "u_alice_123"

    def test_delete_session(self):
        d, us = self._make()
        us.set_history("u_alice", "s1", [{"role": "user", "content": "x"}])
        us.delete_session("u_alice", "s1")
        assert us.get_history("u_alice", "s1") == []
        assert us.list_sessions("u_alice") == []

    def test_clear_user(self):
        d, us = self._make()
        us.set_history("u_alice", "s1", [{"role": "user", "content": "x"}])
        us.set_history("u_alice", "s2", [{"role": "user", "content": "y"}])
        us.clear_user("u_alice")
        assert us.list_sessions("u_alice") == []

    def test_list_sessions(self):
        d, us = self._make()
        us.set_history("u_alice", "s1", [])
        us.set_history("u_alice", "s2", [])
        assert sorted(us.list_sessions("u_alice")) == ["s1", "s2"]
