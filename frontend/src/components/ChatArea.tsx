"use client";

import { useRef, useEffect } from "react";
import MessageBubble from "./MessageBubble";
import ChatInput from "./ChatInput";
import { useChat } from "../providers/ChatProvider";
import type { ChatMessage } from "../lib/types";

// 接收 onOpenSidebar 属性
export default function ChatArea({ onOpenSidebar }: { onOpenSidebar: () => void }) {
  // ★ 新增解构出 sessionId 和 stopGeneration
  const { messages, sendMessage, sse, sessionId, stopGeneration } = useChat();
  const bottomRef = useRef<HTMLDivElement>(null);

  // ★ 核心修改：只有当前会话正在跑，才判定为 busy
  const isCurrentSessionBusy = 
    (sse.status === "connecting" || sse.status === "streaming") && 
    sse.streamSessionId === sessionId;

  useEffect(() => {
    const timer = setTimeout(() => {
      bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }, 100);
    return () => clearTimeout(timer);
    // ★ 依赖里加上 sessionId，切换会话时也要滚到底部
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
    <div className="flex-1 flex flex-col h-screen bg-[#f9f9f9] relative">
      {/* 顶部悬浮栏（仅在侧边栏收起时显示展开按钮） */}
      <div className="absolute top-4 left-4 z-10">
        <button
          onClick={onOpenSidebar}
          className="p-2 bg-white border border-gray-200 rounded-lg shadow-sm text-gray-500 hover:text-gray-900 transition hover:bg-gray-50"
          title="展开侧边栏"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="3" y1="12" x2="21" y2="12" />
            <line x1="3" y1="6" x2="21" y2="6" />
            <line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        </button>
      </div>

      {/* 状态栏 */}
      {sse.progressMessage && isCurrentSessionBusy && (
        <div className="bg-white border-b border-gray-200 px-4 py-2 flex justify-center shrink-0">
          <p className="text-[13px] text-gray-500 flex items-center gap-2">
            <span className="inline-block w-3 h-3 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
            {sse.progressMessage}
          </p>
        </div>
      )}

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto px-4 py-8 pt-16">
        <div className="max-w-[800px] mx-auto w-full">
          {displayMessages.length === 0 && !isCurrentSessionBusy && (
            <div className="flex flex-col items-center justify-center h-full mt-32 text-gray-400">
              <h2 className="text-2xl font-semibold text-gray-800 mb-2">有什么我可以帮你的吗？</h2>
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
      <div className="shrink-0 bg-gradient-to-t from-[#f9f9f9] via-[#f9f9f9] to-transparent pt-4 pb-6 px-4">
        <div className="max-w-[800px] mx-auto">
          {/* ★ 核心修改：传入 onStop，disabled 只受当前会话状态影响 */}
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