"use client";

// 智能搜索助手 — 应用入口页面
import { useEffect, useState } from "react";
import ChatProvider from "@/providers/ChatProvider";
import Sidebar from "@/components/Sidebar";
import ChatArea from "@/components/ChatArea";
import { getConfigDefaults } from "@/lib/api";
import type { UserSettings } from "@/lib/types";

const DEFAULT_SETTINGS: UserSettings = {
  model: "qwen3.7-plus",
  searchDepth: "advanced",
  topK: 5,
};

export default function Home() {
  const [settings, setSettings] = useState<UserSettings>(DEFAULT_SETTINGS);
  const [loaded, setLoaded] = useState(false);
  
  // ★ 新增：控制侧边栏的展开/收起状态（默认展开）
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);

  // 加载服务端默认配置
  useEffect(() => {
    getConfigDefaults()
      .then((cfg) => {
        setSettings({
          model: cfg.model || "qwen3.7-plus",
          searchDepth: cfg.search_depth || "advanced",
          topK: cfg.top_k || 5,
        });
      })
      .catch(() => {
        // 用本地默认值
      })
      .finally(() => setLoaded(true));
  }, []);

  if (!loaded) {
    return (
      <div className="flex items-center justify-center h-screen text-gray-400">
        加载中...
      </div>
    );
  }

  return (
    <ChatProvider settings={settings}>
      {/* 增加背景色，防止收起侧边栏时露出底部黑边 */}
      <div className="flex h-screen overflow-hidden bg-[#f9f9f9]">
        
        {/* 传递状态和关闭回调 */}
        <Sidebar 
          isOpen={isSidebarOpen} 
          onClose={() => setIsSidebarOpen(false)} 
        />
        
        {/* 传递打开回调，用于在左上角显示汉堡菜单 */}
        <ChatArea 
          onOpenSidebar={() => setIsSidebarOpen(true)} 
        />
        
      </div>
    </ChatProvider>
  );
}