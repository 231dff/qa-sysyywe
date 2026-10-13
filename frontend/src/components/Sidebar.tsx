"use client";

// 智能搜索助手 — 侧边栏: 历史会话 + 当前会话
import { useState } from "react";
import { useChat } from "../providers/ChatProvider";
import SettingsModal from "./SettingsModal";

export default function Sidebar() {
  const {
    newSession,
    sessionId,
    sessions,
    sessionsLoading,
    switchSession,
    removeSession,
    sse,
  } = useChat();

  const [settingsOpen, setSettingsOpen] = useState(false);
  const isBusy = sse.status === "connecting" || sse.status === "streaming";

  return (
    <>
      <aside className="w-80 h-screen bg-white border-r border-gray-200 flex flex-col shrink-0">
        {/* ── 顶部：标题 + 设置入口 ── */}
        <div className="p-4 pb-3 flex items-start justify-between">
          <div>
            <h1 className="text-base font-semibold text-gray-900 mb-1">智能搜索助手</h1>
            <p className="text-xs text-gray-500">联网检索 · 知识问答</p>
          </div>
          <button
            onClick={() => setSettingsOpen(true)}
            className="text-gray-400 hover:text-gray-800 transition p-1"
            title="设置"
          >
            {/* 齿轮图标 (SVG) */}
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
        <hr className="border-gray-200" />

        {/* ── 新会话按钮 ── */}
        <div className="p-4 pb-3">
          <button
            onClick={newSession}
            disabled={isBusy}
            className="w-full text-sm bg-gray-900 hover:bg-gray-700 text-white rounded-md px-3 py-2.5 transition disabled:opacity-50"
          >
            + 新会话
          </button>
        </div>
        <hr className="border-gray-200" />

        {/* ── 历史会话列表 ── */}
        <section className="flex-1 overflow-y-auto px-3 py-3">
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2 px-1">
            历史会话
          </h2>

          {sessionsLoading && (
            <p className="text-xs text-gray-400 px-1">加载中…</p>
          )}
          {!sessionsLoading && sessions.length === 0 && (
            <p className="text-xs text-gray-400 px-1">暂无历史会话</p>
          )}

          <div className="space-y-1">
            {sessions.map((s) => {
              const isActive = sessionId === s.session_id;
              return (
                <div
                  key={s.session_id}
                  onClick={() => switchSession(s.session_id)}
                  className={`group p-2.5 rounded-md cursor-pointer transition ${
                    isActive
                      ? "bg-gray-900 text-white"
                      : "hover:bg-gray-100 text-gray-700"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <p
                        className={`text-sm truncate ${
                          isActive ? "text-white" : "text-gray-800"
                        }`}
                      >
                        {s.title}
                      </p>
                      <p
                        className={`text-xs truncate mt-0.5 ${
                          isActive ? "text-gray-300" : "text-gray-400"
                        }`}
                      >
                        {s.preview}
                      </p>
                      <p className="text-xs mt-0.5 text-gray-400">
                        {s.message_count} 条消息
                      </p>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm("确定删除该会话？")) {
                          removeSession(s.session_id);
                        }
                      }}
                      className={`opacity-0 group-hover:opacity-100 text-xs transition ${
                        isActive
                          ? "text-gray-400 hover:text-red-300"
                          : "text-gray-400 hover:text-red-500"
                      }`}
                      title="删除会话"
                    >
                      ✕
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* Footer */}
        <div className="p-3 border-t border-gray-200">
          <p className="text-xs text-gray-300 text-center">
            FastAPI · SSE · Next.js
          </p>
        </div>
      </aside>

      {/* 设置弹窗 */}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </>
  );
}