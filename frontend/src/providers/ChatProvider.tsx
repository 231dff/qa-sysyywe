"use client";

// 智能搜索助手 — 聊天状态管理 Provider
import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import type { ChatMessage, UserSettings, SessionSummary } from "../lib/types";
import { useSSE } from "../hooks/useSSE";
import { generateSessionId, getOrCreateUserId, getSessions, getSession, deleteSession as apiDeleteSession } from "../lib/api";

interface ChatContextType {
  messages: ChatMessage[];
  sessionId: string;
  userId: string;
  settings: UserSettings;
  stats: { totalQueries: number; totalTokens: number };
  // 会话列表
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  // SSE 状态
  sse: ReturnType<typeof useSSE>;
  // 操作
  sendMessage: (content: string) => Promise<void>;
  clearHistory: () => void;
  newSession: () => void;
  updateSettings: (s: Partial<UserSettings>) => void;
  // 会话管理
  refreshSessions: () => Promise<void>;
  switchSession: (sid: string) => Promise<void>;
  removeSession: (sid: string) => Promise<void>;
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
  const [userId, setUserId] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [settings, setSettings] = useState<UserSettings>(defaultSettings);
  const [stats, setStats] = useState({ totalQueries: 0, totalTokens: 0 });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const sse = useSSE();
  const msgCounter = useRef(0);

  // 首次挂载时初始化 userId
  useEffect(() => {
    setUserId(getOrCreateUserId());
  }, []);

  // ── 加载会话列表 ──
  const refreshSessions = useCallback(async () => {
    if (!userId) return;
    setSessionsLoading(true);
    try {
      const data = await getSessions(userId);
      setSessions(data.sessions || []);
    } catch (e) {
      console.error("加载会话列表失败", e);
    } finally {
      setSessionsLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (userId) refreshSessions();
  }, [userId, refreshSessions]);

  // ── 切换会话 ──
  const switchSession = useCallback(
    async (sid: string) => {
      if (!userId) return;
      try {
        const info = await getSession(sid, userId);
        const history: ChatMessage[] = (info.history || []).map(
          (m, i) => ({
            id: `h_${i}`,
            role: m.role as "user" | "assistant",
            content: m.content,
          })
        );
        setMessages(history);
        setSessionId(sid);
        sse.reset();
      } catch (e) {
        console.error("切换会话失败", e);
      }
    },
    [userId, sse]
  );

  // ── 删除会话 ──
  const removeSession = useCallback(
    async (sid: string) => {
      if (!userId) return;
      try {
        await apiDeleteSession(sid, userId);
        if (sid === sessionId) {
          setMessages([]);
          setSessionId(generateSessionId());
        }
        await refreshSessions();
      } catch (e) {
        console.error("删除会话失败", e);
      }
    },
    [userId, sessionId, refreshSessions]
  );

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
      if (!content.trim() || !userId) return;

      const userMsg: ChatMessage = {
        id: `u_${msgCounter.current++}`,
        role: "user",
        content,
      };
      setMessages((prev) => [...prev, userMsg]);

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

  // 监听 SSE 状态变化
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
      // 消息完成后刷新侧边栏
      refreshSessions();
    } else if (sse.status === "error" && sse.error) {
      const errorMsg: ChatMessage = {
        id: `e_${msgCounter.current++}`,
        role: "assistant",
        content: `❌ 错误: ${sse.error}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
      sse.reset();
    } else if (sse.status !== "done" && sse.status !== "error") {
      doneRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sse.status]);

  return (
    <ChatContext.Provider
      value={{
        messages,
        sessionId,
        userId,
        settings,
        stats,
        sessions,
        sessionsLoading,
        sse,
        sendMessage,
        clearHistory,
        newSession,
        updateSettings,
        refreshSessions,
        switchSession,
        removeSession,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}