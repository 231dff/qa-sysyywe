"use client";

// 智能搜索助手 — 侧边栏: 历史会话 + 当前会话
import { useState } from "react";
import { useChat } from "../providers/ChatProvider";
import SettingsModal from "./SettingsModal";

// ★ 新增：接收父组件传来的展开状态和关闭方法
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
    generatingSessionIds, // ★ 接收后台正在生成的会话列表
  } = useChat();

  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <>
      {/* ★ 修改：加入 transition 动画，根据 isOpen 控制宽度和位移 */}
      <aside
        className={`h-screen bg-[#fcfcfc] border-r border-gray-200 flex flex-col shrink-0 transition-all duration-300 ease-in-out ${
          isOpen ? "w-80 translate-x-0" : "w-0 -translate-x-full overflow-hidden border-r-0"
        }`}
      >
        {/* ── 顶部：Logo + 标题 + 设置入口 ── */}
        <div className="p-4 pb-4 flex items-center justify-between min-w-[320px]">
          <div className="flex items-center gap-3">
            {/* 极简 Q&A 气泡 Logo 图标 */}
            <div className="w-9 h-9 rounded-xl bg-[#0f172a] flex items-center justify-center shadow-md shrink-0">
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="white"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
                <circle cx="11" cy="10" r="2.5" />
                <path d="M13.5 12.5L16 15" />
              </svg>
            </div>
            <div className="flex flex-col">
              <h1 className="text-[15px] font-bold text-gray-900 leading-tight mb-0.5">
                智能搜索助手
              </h1>
              <p className="text-[11px] text-gray-500 font-medium">
                联网检索 · 知识问答
              </p>
            </div>
          </div>
          
          <div className="flex items-center gap-1">
            {/* 设置齿轮 */}
            <button
              onClick={() => setSettingsOpen(true)}
              className="text-gray-400 hover:text-gray-800 transition p-1.5 hover:bg-gray-100 rounded-lg"
              title="设置"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
            </button>
            
            {/* 收起侧边栏按钮 */}
            <button
              onClick={onClose}
              className="text-gray-400 hover:text-gray-800 transition p-1.5 hover:bg-gray-100 rounded-lg"
              title="收起侧边栏"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
                <line x1="9" y1="3" x2="9" y2="21" />
              </svg>
            </button>
          </div>
        </div>
        
        <div className="h-px bg-gray-200 mx-4 min-w-[288px]" />

        {/* ── 新会话按钮 ── */}
        <div className="p-4 pb-2 min-w-[320px]">
          <button
            onClick={newSession}
            // ★ 核心修改：去掉了 disabled={isBusy}。允许随时开新会话，不需要等当前会话结束
            className="w-full text-sm font-medium bg-[#0f172a] hover:bg-gray-800 text-white rounded-lg px-3 py-2.5 transition shadow-sm"
          >
            + 新会话
          </button>
        </div>
        
        <div className="h-px bg-gray-200 mx-4 min-w-[288px]" />

        {/* ── 历史会话列表 ── */}
        <section className="flex-1 overflow-y-auto px-3 py-3 mt-2 min-w-[320px]">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2 px-2">
            历史会话
          </h2>

          {sessionsLoading && (
            <p className="text-xs text-gray-400 px-2">加载中…</p>
          )}
          {!sessionsLoading && sessions.length === 0 && (
            <p className="text-xs text-gray-400 px-2">暂无历史会话</p>
          )}

          <div className="space-y-1">
            {sessions.map((s) => {
              const isActive = sessionId === s.session_id;
              // ★ 判断该会话是否在后台正在生成
              const isGenerating = generatingSessionIds.includes(s.session_id);
              
              return (
                <div
                  key={s.session_id}
                  onClick={() => switchSession(s.session_id)}
                  className={`group p-2.5 rounded-lg cursor-pointer transition-all duration-200 ${
                    isActive
                      ? "bg-white border border-gray-200 shadow-sm"
                      : "hover:bg-gray-100 border border-transparent text-gray-700"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <p
                          className={`text-[14px] truncate font-medium ${
                            isActive ? "text-gray-900" : "text-gray-700"
                          }`}
                        >
                          {s.title}
                        </p>
                        {/* ★ 核心新增：如果在后台生成，显示一个呼吸小绿点 */}
                        {isGenerating && (
                          <span className="relative flex h-2.5 w-2.5 shrink-0">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
                            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-green-500"></span>
                          </span>
                        )}
                      </div>
                      <p className="text-xs truncate mt-1 text-gray-400">
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

        {/* Footer */}
        <div className="p-3 border-t border-gray-200 min-w-[320px]">
          <p className="text-[11px] text-gray-400 text-center font-medium">
            FastAPI · SSE · Next.js
          </p>
        </div>
      </aside>

      {/* 设置弹窗 */}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </>
  );
}