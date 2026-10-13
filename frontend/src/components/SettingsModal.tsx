"use client";

// 智能搜索助手 — 设置弹窗
import { useChat } from "../providers/ChatProvider";

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
}

export default function SettingsModal({ open, onClose }: SettingsModalProps) {
  const { settings, updateSettings, sse } = useChat();
  const isBusy = sse.status === "connecting" || sse.status === "streaming";

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题 */}
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-gray-900">设置</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 text-xl leading-none"
            title="关闭"
          >
            ✕
          </button>
        </div>

        {/* 模型 */}
        <label className="block text-sm text-gray-700 mb-1">模型</label>
        <input
          type="text"
          value={settings.model}
          onChange={(e) => updateSettings({ model: e.target.value })}
          disabled={isBusy}
          placeholder="模型名称"
          className="w-full text-sm border border-gray-300 rounded-md px-3 py-2 mb-4 bg-white focus:outline-none focus:ring-2 focus:ring-gray-400 disabled:opacity-50"
        />

        {/* 搜索深度 */}
        <label className="block text-sm text-gray-700 mb-1">搜索深度</label>
        <div className="flex gap-2 mb-4">
          <button
            onClick={() => updateSettings({ searchDepth: "basic" })}
            disabled={isBusy}
            className={`flex-1 text-sm border rounded-md px-3 py-2 transition ${
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
            className={`flex-1 text-sm border rounded-md px-3 py-2 transition ${
              settings.searchDepth === "advanced"
                ? "bg-gray-100 border-gray-400 text-gray-900 font-medium"
                : "bg-white border-gray-300 text-gray-600 hover:bg-gray-50"
            } disabled:opacity-50`}
          >
            深度
          </button>
        </div>

        {/* Top-K */}
        <label className="block text-sm text-gray-700 mb-1">
          Top-K 片段: <span className="font-semibold text-gray-900">{settings.topK}</span>
        </label>
        <input
          type="range"
          min={2}
          max={10}
          value={settings.topK}
          onChange={(e) => updateSettings({ topK: Number(e.target.value) })}
          disabled={isBusy}
          className="w-full mb-6 disabled:opacity-50 accent-gray-600"
        />

        {/* 底部按钮 */}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50 transition"
          >
            完成
          </button>
        </div>

        {isBusy && (
          <p className="text-xs text-gray-400 mt-3 text-center">
            对话进行中，暂时无法修改设置
          </p>
        )}
      </div>
    </div>
  );
}