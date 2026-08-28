"use client";

// 智能搜索助手 — 侧边栏: 设置 + 会话管理
import { useChat } from "../providers/ChatProvider";

export default function Sidebar() {
  const { settings, updateSettings, clearHistory, newSession, sessionId, sse } = useChat();

  const isBusy = sse.status === "connecting" || sse.status === "streaming";

  return (
    <aside className="w-80 h-screen bg-white border-r border-gray-200 flex flex-col p-4 shrink-0">
      {/* 标题 */}
      <h1 className="text-base font-semibold text-gray-900 mb-1">智能搜索助手</h1>
      <p className="text-xs text-gray-500 mb-4">联网检索 · 知识问答</p>
      <hr className="mb-4 border-gray-200" />

      {/* 设置 */}
      <section className="mb-4">
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">设置</h2>

        <label className="block text-xs text-gray-600 mb-1">模型</label>
        <input
          type="text"
          value={settings.model}
          onChange={(e) => updateSettings({ model: e.target.value })}
          disabled={isBusy}
          placeholder="模型名称"
          className="w-full text-sm border border-gray-300 rounded-md px-2.5 py-1.5 mb-3 bg-white focus:outline-none focus:ring-2 focus:ring-gray-400 disabled:opacity-50"
        />

        <label className="block text-xs text-gray-600 mb-1">搜索深度</label>
        <div className="flex gap-2 mb-3">
          <button
            onClick={() => updateSettings({ searchDepth: "basic" })}
            disabled={isBusy}
            className={`flex-1 text-sm border rounded-md px-2 py-1 transition ${
              settings.searchDepth === "basic"
                ? "bg-gray-100 border-gray-400 text-gray-900 font-medium"
                : "bg-white border-gray-300 text-gray-600 hover:bg-gray-50"
            } disabled:opacity-50`}
          >
            基础
          </button>
          <button
            onClick={() => updateSettings({ searchDepth: "advanced" })}
            disabled={isBusy}
            className={`flex-1 text-sm border rounded-md px-2 py-1 transition ${
              settings.searchDepth === "advanced"
                ? "bg-gray-100 border-gray-400 text-gray-900 font-medium"
                : "bg-white border-gray-300 text-gray-600 hover:bg-gray-50"
            } disabled:opacity-50`}
          >
            深度
          </button>
        </div>

        <label className="block text-xs text-gray-600 mb-1">
          Top-K 片段: <span className="font-semibold text-gray-900">{settings.topK}</span>
        </label>
        <input
          type="range"
          min={2}
          max={10}
          value={settings.topK}
          onChange={(e) => updateSettings({ topK: Number(e.target.value) })}
          disabled={isBusy}
          className="w-full mb-4 disabled:opacity-50 accent-gray-600"
        />
      </section>

      <hr className="mb-4 border-gray-200" />

      {/* 会话操作 */}
      <section className="mb-4 flex gap-2">
        <button
          onClick={newSession}
          className="flex-1 text-sm bg-gray-900 hover:bg-gray-700 text-white rounded-md px-3 py-2 transition"
        >
          新会话
        </button>
        <button
          onClick={clearHistory}
          className="flex-1 text-sm bg-white border border-gray-300 hover:bg-gray-50 text-gray-700 rounded-md px-3 py-2 transition"
        >
          清空
        </button>
      </section>

      {/* 会话 ID */}
      <div className="text-xs text-gray-400 truncate">
        会话 <code className="bg-gray-100 px-1 py-0.5 rounded text-gray-600">{sessionId}</code>
      </div>

      {/* Footer */}
      <div className="mt-auto pt-4">
        <p className="text-xs text-gray-300 text-center">
          FastAPI · SSE · Next.js
        </p>
      </div>
    </aside>
  );
}
