"use client";

import { useState } from "react";
import { useChat } from "../providers/ChatProvider";
import SettingsModal from "./SettingsModal";

interface SidebarProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function Sidebar({ isOpen, onClose }: SidebarProps) {
  const {
    newSession,
    sessionId,
    sessions,
    sessionsLoading,
    switchSession,
    removeSession,
    generatingSessionIds,
  } = useChat();

  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <>
      <aside
        className={`h-screen bg-[#fcfcfc] dark:bg-[#19191b] border-r border-gray-200 dark:border-[#2e2e32] flex flex-col shrink-0 transition-all duration-300 ease-in-out ${
          isOpen ? "w-80 translate-x-0" : "w-0 -translate-x-full overflow-hidden border-r-0"
        }`}
      >
        <div className="p-4 pb-4 flex items-center justify-between min-w-[320px]">
          <div className="flex items-center gap-3">
            {/* ★★★ 全新设计的绝美 Logo：蓝紫渐变背景 + 闪耀四芒星 ★★★ */}
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-[0_4px_12px_rgba(59,130,246,0.4)] shrink-0 transition-transform hover:scale-105">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="white">
                <path d="M12 2C12 2 13.5 8.5 22 10C13.5 11.5 12 18 12 18C12 18 10.5 11.5 2 10C10.5 8.5 12 2 12 2Z" />
              </svg>
            </div>
            <div className="flex flex-col">
              <h1 className="text-[15px] font-bold text-gray-900 dark:text-gray-100 leading-tight mb-0.5 transition-colors">
                智能搜索助手
              </h1>
              <p className="text-[11px] text-gray-500 dark:text-gray-400 font-medium transition-colors">
                联网检索 · 知识问答
              </p>
            </div>
          </div>
          
          <div className="flex items-center gap-1">
            <button
              onClick={() => setSettingsOpen(true)}
              className="text-gray-400 hover:text-gray-800 dark:text-gray-500 dark:hover:text-gray-200 transition p-1.5 hover:bg-gray-100 dark:hover:bg-[#2a2a2d] rounded-lg"
              title="设置"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
            </button>
            <button
              onClick={onClose}
              className="text-gray-400 hover:text-gray-800 dark:text-gray-500 dark:hover:text-gray-200 transition p-1.5 hover:bg-gray-100 dark:hover:bg-[#2a2a2d] rounded-lg"
              title="收起侧边栏"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
                <line x1="9" y1="3" x2="9" y2="21" />
              </svg>
            </button>
          </div>
        </div>
        
        <div className="h-px bg-gray-200 dark:bg-[#2e2e32] mx-4 min-w-[288px] transition-colors" />

        <div className="p-4 pb-2 min-w-[320px]">
          <button
            onClick={newSession}
            className="w-full text-sm font-medium bg-[#0f172a] hover:bg-gray-800 dark:bg-[#2a2a2d] dark:hover:bg-[#3a3a3d] text-white rounded-lg px-3 py-2.5 transition shadow-sm"
          >
            + 新会话
          </button>
        </div>
        
        <div className="h-px bg-gray-200 dark:bg-[#2e2e32] mx-4 min-w-[288px] transition-colors" />

        <section className="flex-1 overflow-y-auto px-3 py-3 mt-2 min-w-[320px] custom-scrollbar">
          <h2 className="text-xs font-semibold text-gray-400 dark:text-gray-500 uppercase tracking-wide mb-2 px-2 transition-colors">
            历史会话
          </h2>

          {sessionsLoading && (
            <p className="text-xs text-gray-400 dark:text-gray-500 px-2">加载中…</p>
          )}
          {!sessionsLoading && sessions.length === 0 && (
            <p className="text-xs text-gray-400 dark:text-gray-500 px-2">暂无历史会话</p>
          )}

          <div className="space-y-1">
            {sessions.map((s) => {
              const isActive = sessionId === s.session_id;
              const isGenerating = generatingSessionIds.includes(s.session_id);
              
              return (
                <div
                  key={s.session_id}
                  onClick={() => switchSession(s.session_id)}
                  className={`group p-2.5 rounded-lg cursor-pointer transition-all duration-200 ${
                    isActive
                      ? "bg-white dark:bg-[#2a2a2d] border border-gray-200 dark:border-[#3e3e42] shadow-sm"
                      : "hover:bg-gray-100 dark:hover:bg-[#252527] border border-transparent text-gray-700 dark:text-gray-300"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <p
                          className={`text-[14px] truncate font-medium ${
                            isActive ? "text-gray-900 dark:text-gray-100" : "text-gray-700 dark:text-gray-300"
                          } transition-colors`}
                        >
                          {s.title}
                        </p>
                        {isGenerating && (
                          <span className="relative flex h-2.5 w-2.5 shrink-0">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
                            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-green-500"></span>
                          </span>
                        )}
                      </div>
                      <p className="text-xs truncate mt-1 text-gray-400 dark:text-gray-500 transition-colors">
                        {s.preview}
                      </p>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm("确定删除该会话？")) {
                          removeSession(s.session_id);
                        }
                      }}
                      className={`opacity-0 group-hover:opacity-100 text-xs transition p-1 text-gray-400 hover:text-red-500`}
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

        <div className="p-3 border-t border-gray-200 dark:border-[#2e2e32] min-w-[320px] transition-colors">
          <p className="text-[11px] text-gray-400 dark:text-gray-600 text-center font-medium">
            FastAPI · SSE · Next.js
          </p>
        </div>
      </aside>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </>
  );
}