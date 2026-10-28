"use client";

import { useState, useEffect } from "react";
import { useChat } from "../providers/ChatProvider";

export default function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { theme, toggleTheme } = useChat(); // ★ 获取全局主题状态与切换方法

  const [provider, setProvider] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [mcpConfig, setMcpConfig] = useState(""); 

  useEffect(() => {
    if (open) {
      const saved = localStorage.getItem("custom_llm_config");
      if (saved) {
        try {
          const parsed = JSON.parse(saved);
          setProvider(parsed.provider || "");
          setApiBase(parsed.apiBase || "");
          setApiKey(parsed.apiKey || "");
          setModel(parsed.model || "");
        } catch (e) {}
      }
      const savedMcp = localStorage.getItem("custom_mcp_config");
      if (savedMcp) setMcpConfig(savedMcp);
    }
  }, [open]);

  const handleSave = () => {
    const config = { provider, apiBase, apiKey, model };
    localStorage.setItem("custom_llm_config", JSON.stringify(config));
    localStorage.setItem("custom_mcp_config", mcpConfig);
    onClose();
  };

  const handleClear = () => {
    localStorage.removeItem("custom_llm_config");
    localStorage.removeItem("custom_mcp_config");
    setProvider("");
    setApiBase("");
    setApiKey("");
    setModel("");
    setMcpConfig("");
    onClose();
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 dark:bg-black/70 backdrop-blur-sm transition-colors">
      <div className="w-[480px] rounded-xl bg-white dark:bg-[#2a2a2d] p-6 shadow-xl max-h-[90vh] overflow-y-auto transition-colors border border-transparent dark:border-gray-700">
        <h2 className="mb-4 text-lg font-bold text-gray-800 dark:text-gray-100 transition-colors">
          自定义大模型与工具设置
        </h2>
        
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300 transition-colors">
              Provider (如 openai, deepseek)
            </label>
            <input
              type="text"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="留空则使用后端默认配置"
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-transparent dark:bg-[#1e1e20] px-3 py-2 text-sm text-gray-900 dark:text-gray-200 focus:border-gray-900 dark:focus:border-gray-400 focus:outline-none transition-colors"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300 transition-colors">
              API Base URL
            </label>
            <input
              type="text"
              value={apiBase}
              onChange={(e) => setApiBase(e.target.value)}
              placeholder="如 https://api.deepseek.com/v1"
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-transparent dark:bg-[#1e1e20] px-3 py-2 text-sm text-gray-900 dark:text-gray-200 focus:border-gray-900 dark:focus:border-gray-400 focus:outline-none transition-colors"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300 transition-colors">
              API Key
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-transparent dark:bg-[#1e1e20] px-3 py-2 text-sm text-gray-900 dark:text-gray-200 focus:border-gray-900 dark:focus:border-gray-400 focus:outline-none transition-colors"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300 transition-colors">
              模型名称
            </label>
            <input
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="如 deepseek-chat, qwen-max"
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-transparent dark:bg-[#1e1e20] px-3 py-2 text-sm text-gray-900 dark:text-gray-200 focus:border-gray-900 dark:focus:border-gray-400 focus:outline-none transition-colors"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300 transition-colors">
              自定义 MCP Server 配置 (JSON)
            </label>
            <textarea
              rows={4}
              value={mcpConfig}
              onChange={(e) => setMcpConfig(e.target.value)}
              placeholder='例如: {"weather": {"command": "python", "args": ["/app/mcp_server.py"], "transport": "stdio"}}'
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-transparent dark:bg-[#1e1e20] px-3 py-2 text-sm text-gray-900 dark:text-gray-200 focus:border-gray-900 dark:focus:border-gray-400 focus:outline-none font-mono transition-colors"
            />
          </div>
        </div>

        {/* ★ 新增：外观设置区域 */}
        <div className="mt-6 border-t border-gray-200 dark:border-gray-700 pt-5">
          <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-3 transition-colors">
            外观设置
          </h3>
          <div className="flex items-center justify-between bg-gray-50 dark:bg-[#1e1e20] p-3 rounded-lg border border-gray-200 dark:border-gray-700 transition-colors">
            <div className="flex flex-col">
              <span className="text-sm font-medium text-gray-800 dark:text-gray-200 transition-colors">
                主题模式
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400 transition-colors">
                切换深色或浅色背景
              </span>
            </div>
            
            {/* 切换开关 */}
            <button
              onClick={toggleTheme}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 dark:focus:ring-offset-gray-900 ${
                theme === "dark" ? "bg-[#0f172a]" : "bg-gray-300"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  theme === "dark" ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        </div>

        {/* 底部按钮 */}
        <div className="mt-6 flex justify-between items-center">
          <button
            onClick={handleClear}
            className="rounded-md px-4 py-2 text-sm font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 transition"
          >
            清空配置
          </button>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-md px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-[#3a3a3d] transition"
            >
              取消
            </button>
            <button
              onClick={handleSave}
              className="rounded-md bg-gray-900 dark:bg-gray-100 px-4 py-2 text-sm font-medium text-white dark:text-gray-900 hover:bg-gray-700 dark:hover:bg-gray-300 transition"
            >
              保存配置
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}