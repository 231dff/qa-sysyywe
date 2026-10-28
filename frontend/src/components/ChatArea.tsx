"use client";

import { useRef, useEffect } from "react";
import MessageBubble from "./MessageBubble";
import ChatInput from "./ChatInput";
import { useChat } from "../providers/ChatProvider";
import type { ChatMessage } from "../lib/types";

export default function ChatArea({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  const { messages, sendMessage, sse, sessionId, stopGeneration, theme, toggleTheme } = useChat();
  const bottomRef = useRef<HTMLDivElement>(null);

  const isCurrentSessionBusy = 
    (sse.status === "connecting" || sse.status === "streaming") && 
    sse.streamSessionId === sessionId;

  useEffect(() => {
    const timer = setTimeout(() => {
      bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }, 100);
    return () => clearTimeout(timer);
  }, [messages, sse.tokens, sse.status, sessionId]);

  const streamingMsg: ChatMessage | null = isCurrentSessionBusy
    ? {
        id: "streaming",
        role: "assistant" as const,
        content: sse.tokens,
        sources: sse.sources.length > 0 ? sse.sources : undefined,
        meta: sse.meta ?? undefined,
      }
    : null;

  const displayMessages = streamingMsg ? [...messages, streamingMsg] : messages;

  return (
    <div className="flex-1 flex flex-col h-screen bg-[#f9f9f9] dark:bg-[#1e1e20] relative transition-colors duration-300">
      {/* 顶部悬浮栏 */}
      <div className="absolute top-4 left-4 z-10 flex items-center gap-2">
        {/* ★ 幽灵按钮：去掉了白底和边框，透明悬浮 */}
        <button
          onClick={onOpenSidebar}
          className="p-2 bg-transparent hover:bg-black/5 dark:hover:bg-white/10 rounded-lg text-gray-400 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-200 transition-all"
          title="展开侧边栏"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>
      </div>

      {/* 右上角主题切换 */}
      <div className="absolute top-4 right-4 z-10">
        {/* ★ 幽灵按钮：无边框、圆形背景仅在悬浮时出现，图标换成极简风格 */}
        <button
          onClick={toggleTheme}
          className="p-2.5 bg-transparent hover:bg-black/5 dark:hover:bg-white/10 rounded-full text-gray-400 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-200 transition-all duration-200"
          title={theme === "light" ? "切换到暗黑模式" : "切换到浅色模式"}
        >
          {theme === "light" ? (
            // 极简月亮
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
          ) : (
            // 极简太阳
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
          )}
        </button>
      </div>

      {/* 状态栏 */}
      {sse.progressMessage && isCurrentSessionBusy && (
        <div className="bg-white dark:bg-[#2a2a2d] border-b border-gray-200 dark:border-gray-700 px-4 py-2 flex justify-center shrink-0 transition-colors">
          <div className="flex items-center gap-3 max-w-[800px] w-full truncate">
            <p className="text-[13px] text-gray-600 dark:text-gray-300 flex items-center gap-2 shrink-0">
              <span className="inline-block w-3 h-3 border-2 border-gray-400 dark:border-gray-500 border-t-transparent rounded-full animate-spin" />
              {sse.progressMessage}
            </p>
            
            {sse.reasoningMessage && (
              <div className="flex items-center gap-2 overflow-hidden truncate text-[12px] text-gray-400 dark:text-gray-500 border-l border-gray-300 dark:border-gray-600 pl-3">
                <span className="shrink-0 text-gray-500">思考中:</span>
                <span className="truncate italic">{sse.reasoningMessage}...</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto px-4 py-8 pt-16 custom-scrollbar">
        <div className="max-w-[800px] mx-auto w-full">
          {displayMessages.length === 0 && !isCurrentSessionBusy && (
            <div className="flex flex-col items-center justify-center h-full mt-32 text-gray-400 dark:text-gray-500">
              <h2 className="text-2xl font-semibold text-gray-800 dark:text-gray-200 mb-2">有什么我可以帮你的吗？</h2>
              <p className="text-sm">输入问题开始检索，支持联网与知识图谱。</p>
            </div>
          )}
          {displayMessages.map((msg) => (
            <MessageBubble key={msg.id} msg={msg} isStreaming={msg.id === "streaming" && isCurrentSessionBusy} />
          ))}
          <div ref={bottomRef} className="h-4" />
        </div>
      </div>

      {/* 输入框容器 */}
      <div className="shrink-0 bg-gradient-to-t from-[#f9f9f9] via-[#f9f9f9] dark:from-[#1e1e20] dark:via-[#1e1e20] to-transparent pt-4 pb-6 px-4 transition-colors">
        <div className="max-w-[800px] mx-auto">
          <ChatInput 
            onSubmit={sendMessage} 
            onStop={stopGeneration} 
            disabled={isCurrentSessionBusy} 
          />
        </div>
      </div>
    </div>
  );
}