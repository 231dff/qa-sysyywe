"use client";

import { useState, useEffect } from "react";

export default function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [provider, setProvider] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [mcpConfig, setMcpConfig] = useState(""); // ★ 新增 MCP 配置

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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-[480px] rounded-xl bg-white p-6 shadow-xl max-h-[90vh] overflow-y-auto">
        <h2 className="mb-4 text-lg font-bold text-gray-800">自定义大模型与工具设置</h2>
        
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">Provider (如 openai, deepseek)</label>
            <input
              type="text"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              placeholder="留空则使用后端默认配置"
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">API Base URL</label>
            <input
              type="text"
              value={apiBase}
              onChange={(e) => setApiBase(e.target.value)}
              placeholder="如 https://api.deepseek.com/v1"
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">模型名称</label>
            <input
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="如 deepseek-chat, qwen-max"
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">自定义 MCP Server 配置 (JSON)</label>
            <textarea
              rows={4}
              value={mcpConfig}
              onChange={(e) => setMcpConfig(e.target.value)}
              placeholder='例如: {"weather": {"command": "python", "args": ["/app/mcp_server.py"], "transport": "stdio"}}'
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-900 focus:outline-none font-mono"
            />
          </div>
        </div>

        <div className="mt-6 flex justify-between">
          <button
            onClick={handleClear}
            className="rounded-md px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 transition"
          >
            清空配置
          </button>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-md px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 transition"
            >
              取消
            </button>
            <button
              onClick={handleSave}
              className="rounded-md bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-700 transition"
            >
              保存配置
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}