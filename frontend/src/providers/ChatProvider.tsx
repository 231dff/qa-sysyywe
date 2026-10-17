"use client";

// 智能搜索助手 — 聊天状态管理 Provider (前端自管历史会话版)
import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import type { ChatMessage, UserSettings, SessionSummary } from "../lib/types";
import { useSSE } from "../hooks/useSSE";
import { generateSessionId, getOrCreateUserId, getSession, deleteSession as apiDeleteSession } from "../lib/api";

const LOCAL_SESSIONS_KEY = "qa_local_sessions_v1";

// ── 本地存储辅助函数 ──
const getLocalSessions = (): SessionSummary[] => {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(LOCAL_SESSIONS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
};

const saveLocalSessions = (data: SessionSummary[]) => {
  if (typeof window === "undefined") return;
  localStorage.setItem(LOCAL_SESSIONS_KEY, JSON.stringify(data));
};

interface ChatContextType {
  messages: ChatMessage[];
  sessionId: string;
  userId: string;
  settings: UserSettings;
  stats: { totalQueries: number; totalTokens: number };
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  sse: ReturnType<typeof useSSE>;
  sendMessage: (content: string) => Promise<void>;
  clearHistory: () => void;
  newSession: () => void;
  updateSettings: (s: Partial<UserSettings>) => void;
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
  
  // 会话列表现在完全由前端维护
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  
  const sse = useSSE();
  const msgCounter = useRef(0);

  // 初始化 userId 和 本地列表
  useEffect(() => {
    setUserId(getOrCreateUserId());
    setSessions(getLocalSessions());
  }, []);

  // 刷新本地列表
  const refreshSessions = useCallback(async () => {
    setSessions(getLocalSessions());
  }, []);

  // ── 切换会话 ──
  const switchSession = useCallback(
    async (sid: string) => {
      if (!userId) return;
      setSessionsLoading(true);
      try {
        // 依然从后端(Redis)拉取该会话的历史记录
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
      } finally {
        setSessionsLoading(false);
      }
    },
    [userId, sse]
  );

  // ── 删除会话 ──
  const removeSession = useCallback(
    async (sid: string) => {
      if (!userId) return;
      try {
        // 通知后端删除
        await apiDeleteSession(sid, userId);
        
        // 从前端本地列表中移除
        const current = getLocalSessions().filter((s) => s.session_id !== sid);
        saveLocalSessions(current);
        setSessions(current);

        if (sid === sessionId) {
          setMessages([]);
          setSessionId(generateSessionId());
        }
      } catch (e) {
        console.error("删除会话失败", e);
      }
    },
    [userId, sessionId]
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

  // ── 发送消息 ──
  const sendMessage = useCallback(
    async (content: string) => {
      if (!content.trim() || !userId) return;

      // 1. 更新前端会话列表
      let currentSessions = getLocalSessions();
      const exists = currentSessions.find((s) => s.session_id === sessionId);

      if (!exists) {
        // 新会话，加入列表
        const newSession: SessionSummary = {
          session_id: sessionId,
          title: content.slice(0, 20) || "新对话", // 截取前20字当标题
          preview: content.slice(0, 50),
          message_count: 1,
        };
        currentSessions = [newSession, ...currentSessions];
      } else {
        // 已有会话，更新预览并移到最前
        currentSessions = currentSessions.filter((s) => s.session_id !== sessionId);
        exists.preview = content.slice(0, 50);
        exists.message_count = (exists.message_count || 0) + 1;
        currentSessions = [exists, ...currentSessions];
      }
      saveLocalSessions(currentSessions);
      setSessions(currentSessions);

      // 2. 发送 SSE 请求
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