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
        reasoningText: sse.reasoningText,
        reasoningDuration: sse.reasoningDuration,
        isThinking: sse.isThinking,
        progressMessage: sse.progressMessage,
      }
    : null;

  const displayMessages = streamingMsg ? [...messages, streamingMsg] : messages;

  return (
    <div className="flex-1 flex flex-col h-screen bg-[#f9f9f9] dark:bg-[#1e1e20] relative transition-colors duration-300">
      {/* 顶部悬浮栏 */}
      <div className="absolute top-4 left-4 z-10 flex items-center gap-2">
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
        <button
          onClick={toggleTheme}
          className="p-2.5 bg-transparent hover:bg-black/5 dark:hover:bg-white/10 rounded-full text-gray-400 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-200 transition-all duration-200"
          title={theme === "light" ? "切换到暗黑模式" : "切换到浅色模式"}
        >
          {theme === "light" ? (
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
          )}
        </button>
      </div>

      {/* 消息列表 / 欢迎页 */}
      <div className="flex-1 overflow-y-auto px-4 py-8 pt-16 custom-scrollbar flex flex-col">
        <div className="max-w-[800px] mx-auto w-full flex-1 flex flex-col">
          {displayMessages.length === 0 && !isCurrentSessionBusy ? (
            // ★ DeepSeek 风格的欢迎页
            <div className="flex flex-col items-center justify-center flex-1 mt-[-10vh]">
              <div className="flex items-center gap-3 mb-6">
                {/* Logo */}
                <div className="w-12 h-12 rounded-2xl bg-blue-500 flex items-center justify-center shadow-lg">
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="white">
                    <path d="M12 2C12 2 13.5 8.5 22 10C13.5 11.5 12 18 12 18C12 18 10.5 11.5 2 10C10.5 8.5 12 2 12 2Z" />
                  </svg>
                </div>
                <h2 className="text-[32px] font-semibold text-gray-800 dark:text-gray-100">
                  你好，让我们开始聊天吧
                </h2>
              </div>
              <p className="text-gray-500 dark:text-gray-400 text-[15px]">
                支持深度思考、实时联网搜索与多轮对话。
              </p>
            </div>
          ) : (
            // 消息列表
            <>
              {displayMessages.map((msg) => (
                <MessageBubble key={msg.id} msg={msg} isStreaming={msg.id === "streaming" && isCurrentSessionBusy} />
              ))}
              <div ref={bottomRef} className="h-4" />
            </>
          )}
        </div>
      </div>

      {/* 输入框容器 */}
      <div className="shrink-0 bg-gradient-to-t from-[#f9f9f9] via-[#f9f9f9] dark:from-[#1e1e20] dark:via-[#1e1e20] to-transparent pt-4 pb-6 px-4 transition-colors">
        <div className="max-w-[800px] mx-auto w-full">
          <ChatInput 
            onSubmit={sendMessage} 
            onStop={stopGeneration} 
            disabled={isCurrentSessionBusy} 
          />
          <div className="text-center mt-2 text-[12px] text-gray-400 dark:text-gray-500">
            内容由 AI 生成，请仔细甄别。
          </div>
        </div>
      </div>
    </div>
  );
}