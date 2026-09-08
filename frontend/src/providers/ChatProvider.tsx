"use client";

// 智能搜索助手 — 聊天状态管理 Provider
import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import type { ChatMessage, UserSettings } from "../lib/types";
import { useSSE } from "../hooks/useSSE";
import { generateSessionId, getOrCreateUserId } from "../lib/api";

interface ChatContextType {
  messages: ChatMessage[];
  sessionId: string;
  userId: string;
  settings: UserSettings;
  stats: { totalQueries: number; totalTokens: number };
  // SSE 状态
  sse: ReturnType<typeof useSSE>;
  // 操作
  sendMessage: (content: string) => Promise<void>;
  clearHistory: () => void;
  newSession: () => void;
  updateSettings: (s: Partial<UserSettings>) => void;
}

const ChatContext = createContext<ChatContextType | null>(null);

export function useChat() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChat 必须在 ChatProvider 内使用");
  return ctx;
}

export default function ChatProvider({
  settings: defaultSettings,
  children,
}: {
  settings: UserSettings;
  children: React.ReactNode;
}) {
  const [sessionId, setSessionId] = useState<string>(generateSessionId);
  const [userId] = useState<string>(getOrCreateUserId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [settings, setSettings] = useState<UserSettings>(defaultSettings);
  const [stats, setStats] = useState({ totalQueries: 0, totalTokens: 0 });
  const sse = useSSE();
  const msgCounter = useRef(0);

  const clearHistory = useCallback(() => {
    setMessages([]);
    sse.reset();
  }, [sse]);

  const newSession = useCallback(() => {
    setMessages([]);
    sse.reset();
    setSessionId(generateSessionId());
  }, [sse]);

  const updateSettings = useCallback((s: Partial<UserSettings>) => {
    setSettings((prev) => ({ ...prev, ...s }));
  }, []);

  const sendMessage = useCallback(
    async (content: string) => {
      if (!content.trim()) return;

      const userMsg: ChatMessage = {
        id: `u_${msgCounter.current++}`,
        role: "user",
        content,
      };
      setMessages((prev) => [...prev, userMsg]);

      // 启动 SSE
      await sse.startStream({
        query: content,
        session_id: sessionId,
        user_id: userId,
        model: settings.model,
        search_depth: settings.searchDepth,
        top_k: settings.topK,
      });
    },
    [sessionId, userId, settings, sse]
  );

  // 监听 SSE 状态变化 — 仅在 done/error 时将助手消息加入 messages（只加一次）
  const doneRef = useRef(false);
  useEffect(() => {
    if (sse.status === "done" && !doneRef.current && sse.meta) {
      doneRef.current = true;
      const assistantMsg: ChatMessage = {
        id: `a_${msgCounter.current++}`,
        role: "assistant",
        content: sse.tokens,
        sources: sse.sources,
        meta: sse.meta,
      };
      setMessages((prev) => [...prev, assistantMsg]);
      setStats((prev) => ({
        totalQueries: prev.totalQueries + 1,
        totalTokens: prev.totalTokens + (sse.meta?.tokens_used ?? 0),
      }));
    } else if (sse.status === "error" && sse.error) {
      const errorMsg: ChatMessage = {
        id: `e_${msgCounter.current++}`,
        role: "assistant",
        content: `❌ 错误: ${sse.error}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
      sse.reset(); // 清除错误状态
    } else if (sse.status !== "done" && sse.status !== "error") {
      doneRef.current = false; // 新请求开始时重置
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只监听 status 变化；tokens/sources/meta 在 done 时已定格，纳入依赖会触发重复添加
  }, [sse.status]);

  return (
    <ChatContext.Provider
      value={{
        messages,
        sessionId,
        userId,
        settings,
        stats,
        sse,
        sendMessage,
        clearHistory,
        newSession,
        updateSettings,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}
