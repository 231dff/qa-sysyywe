"use client";

import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import type { ChatMessage, UserSettings, SessionSummary, ChatRequest, SourceInfo, DoneEvent } from "../lib/types";
import { generateSessionId, getOrCreateUserId, getSession, deleteSession as apiDeleteSession } from "../lib/api";

const SSE_BASE = process.env.NEXT_PUBLIC_BACKEND_URL ?? "";
const LOCAL_SESSIONS_KEY = "qa_local_sessions_v1";

// 定义单个会话的 SSE 状态
export interface SessionSSEState {
  status: "idle" | "connecting" | "streaming" | "done" | "error" | "aborted";
  tokens: string;
  sources: SourceInfo[];
  meta: DoneEvent | null;
  error: string | null;
  progressMessage: string | null;
}

const DEFAULT_SSE_STATE: SessionSSEState = {
  status: "idle", tokens: "", sources: [], meta: null, error: null, progressMessage: null,
};

const getLocalSessions = (): SessionSummary[] => {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(LOCAL_SESSIONS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
};

const saveLocalSessions = (data: SessionSummary[]) => {
  if (typeof window === "undefined") return;
  localStorage.setItem(LOCAL_SESSIONS_KEY, JSON.stringify(data));
};

interface ChatContextType {
  messages: ChatMessage[]; // 当前激活会话的消息
  sessionId: string;
  userId: string;
  settings: UserSettings;
  stats: { totalQueries: number; totalTokens: number };
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  sse: SessionSSEState & { streamSessionId: string }; // 当前激活会话的 SSE 状态
  generatingSessionIds: string[]; // ★ 新增：记录所有正在后台运行的会话 ID
  sendMessage: (content: string) => Promise<void>;
  stopGeneration: () => void;
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
  
  // ★ 核心重构：消息字典和多会话 SSE 字典
  const [messagesMap, setMessagesMap] = useState<Record<string, ChatMessage[]>>({});
  const [sseStateMap, setSseStateMap] = useState<Record<string, SessionSSEState>>({});
  
  const [settings, setSettings] = useState<UserSettings>(defaultSettings);
  const [stats, setStats] = useState({ totalQueries: 0, totalTokens: 0 });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // 并发控制器字典，记录每个会话的 AbortController
  const abortControllers = useRef<Record<string, AbortController>>({});
  const msgCounter = useRef(0);

  // 当前会话的数据（提供给 UI 消费）
  const currentMessages = messagesMap[sessionId] || [];
  const currentSseState = sseStateMap[sessionId] || DEFAULT_SSE_STATE;

  // ★ 计算所有正在后台生成的会话 ID
  const generatingSessionIds = Object.keys(sseStateMap).filter(
    (sid) => sseStateMap[sid].status === "connecting" || sseStateMap[sid].status === "streaming"
  );

  useEffect(() => {
    setUserId(getOrCreateUserId());
    setSessions(getLocalSessions());
  }, []);

  const refreshSessions = useCallback(async () => {
    setSessions(getLocalSessions());
  }, []);

  const switchSession = useCallback(
    async (sid: string) => {
      if (!userId || sid === sessionId) return;
      
      setSessionId(sid); // 瞬间切换 UI
      
      // 如果本地 Map 里没有该会话数据，去后端拉取
      if (!messagesMap[sid] || messagesMap[sid].length === 0) {
        setSessionsLoading(true);
        try {
          const info = await getSession(sid, userId);
          const history: ChatMessage[] = (info.history || []).map((m, i) => ({
            id: `h_${i}`, role: m.role as "user" | "assistant", content: m.content,
          }));
          setMessagesMap(prev => ({ ...prev, [sid]: history }));
        } catch (e: any) {
          console.error("切换会话失败", e);
          setMessagesMap(prev => ({
            ...prev,
            [sid]: [{ id: `err_${Date.now()}`, role: "assistant", content: `⚠️ 无法加载该会话：${e.message || "网络错误"}` }]
          }));
        } finally {
          setSessionsLoading(false);
        }
      }
    },
    [userId, sessionId, messagesMap]
  );

  const removeSession = useCallback(
    async (sid: string) => {
      if (!userId) return;
      try {
        await apiDeleteSession(sid, userId);
        const current = getLocalSessions().filter((s) => s.session_id !== sid);
        saveLocalSessions(current);
        setSessions(current);
        
        if (sid === sessionId) {
          setSessionId(generateSessionId());
        }
        // 清理内存
        setMessagesMap(prev => {
          const next = { ...prev };
          delete next[sid];
          return next;
        });
      } catch (e) { console.error("删除会话失败", e); }
    },
    [userId, sessionId]
  );

  const clearHistory = useCallback(() => {
    setMessagesMap(prev => ({ ...prev, [sessionId]: [] }));
    setSseStateMap(prev => ({ ...prev, [sessionId]: DEFAULT_SSE_STATE }));
    if (abortControllers.current[sessionId]) {
      abortControllers.current[sessionId].abort();
      delete abortControllers.current[sessionId];
    }
  }, [sessionId]);

  const newSession = useCallback(() => {
    setSessionId(generateSessionId());
  }, []);

  const updateSettings = useCallback((s: Partial<UserSettings>) => {
    setSettings((prev) => ({ ...prev, ...s }));
  }, []);

  const sendMessage = useCallback(
    async (content: string) => {
      if (!content.trim() || !userId) return;
      const sid = sessionId;

      // 如果当前会话还在跑，先强行停掉（单会话内不允许并发）
      if (abortControllers.current[sid]) {
        abortControllers.current[sid].abort();
      }

      // 更新本地历史列表
      let currentSessions = getLocalSessions();
      const exists = currentSessions.find((s) => s.session_id === sid);
      if (!exists) {
        currentSessions = [{ session_id: sid, title: content.slice(0, 20) || "新对话", preview: content.slice(0, 50), message_count: 1 }, ...currentSessions];
      } else {
        currentSessions = currentSessions.filter((s) => s.session_id !== sid);
        exists.preview = content.slice(0, 50);
        exists.message_count = (exists.message_count || 0) + 1;
        currentSessions = [exists, ...currentSessions];
      }
      saveLocalSessions(currentSessions);
      setSessions(currentSessions);

      // 追加用户消息
      const userMsg: ChatMessage = { id: `u_${msgCounter.current++}`, role: "user", content };
      setMessagesMap(prev => ({ ...prev, [sid]: [...(prev[sid] || []), userMsg] }));

      // 初始化 SSE 状态
      setSseStateMap(prev => ({
        ...prev,
        [sid]: { ...DEFAULT_SSE_STATE, status: "connecting", progressMessage: "正在思考如何回答..." }
      }));

      let customConfig: any = {};
      let customMcp: any = {};
      try {
        const saved = localStorage.getItem("custom_llm_config");
        if (saved) customConfig = JSON.parse(saved);
        const savedMcp = localStorage.getItem("custom_mcp_config");
        if (savedMcp) customMcp = JSON.parse(savedMcp);
      } catch (e) {}

      const request: ChatRequest = {
        query: content, session_id: sid, user_id: userId,
        model: customConfig.model || settings.model,
        llm_provider: customConfig.provider || undefined,
        api_base: customConfig.apiBase || undefined,
        api_key: customConfig.apiKey || undefined,
        mcp_config: Object.keys(customMcp).length > 0 ? customMcp : undefined,
        search_depth: settings.searchDepth, top_k: settings.topK,
      };

      // 启动流
      const controller = new AbortController();
      abortControllers.current[sid] = controller;

      try {
        const res = await fetch(`${SSE_BASE}/api/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
          signal: controller.signal,
        });

        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
        const reader = res.body?.getReader();
        if (!reader) throw new Error("无法读取响应流");

        const decoder = new TextDecoder();
        let buffer = "";
        let currentEvent = "";
        
        setSseStateMap(prev => ({ ...prev, [sid]: { ...prev[sid], status: "streaming" } }));

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (line === "") { currentEvent = ""; continue; }
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              const dataStr = line.slice(5).trim();
              if (currentEvent && dataStr) {
                try {
                  const data = JSON.parse(dataStr);
                  // ★ 核心：通过闭包捕获的 sid，把状态更新到对应会话！
                  handleSSEEvent(sid, currentEvent, data, setSseStateMap);
                } catch {}
              }
              currentEvent = "";
            }
          }
        }

        // 流正常结束
        setSseStateMap(prev => ({ ...prev, [sid]: { ...prev[sid], status: "done" } }));
      } catch (err: any) {
        if (err.name === "AbortError") {
          setSseStateMap(prev => ({ ...prev, [sid]: { ...prev[sid], status: "aborted" } }));
        } else {
          setSseStateMap(prev => ({ ...prev, [sid]: { ...prev[sid], status: "error", error: err.message } }));
        }
      } finally {
        delete abortControllers.current[sid];
      }
    },
    [sessionId, userId, settings]
  );

  const stopGeneration = useCallback(() => {
    if (abortControllers.current[sessionId]) {
      abortControllers.current[sessionId].abort();
    }
  }, [sessionId]);

  // 处理流结束时，把 tokens 固化到 messages 里
  const doneRef = useRef<Record<string, boolean>>({});
  useEffect(() => {
    Object.keys(sseStateMap).forEach(sid => {
      const sse = sseStateMap[sid];
      
      if (sse.status === "done" && !doneRef.current[sid]) {
        doneRef.current[sid] = true;
        // 固化消息
        setMessagesMap(prev => {
          const msgs = prev[sid] || [];
          const assistantMsg: ChatMessage = {
            id: `a_${msgCounter.current++}`, role: "assistant",
            content: sse.tokens, sources: sse.sources, meta: sse.meta || undefined,
          };
          return { ...prev, [sid]: [...msgs, assistantMsg] };
        });
        
        if (sid === sessionId) {
          setStats(prev => ({
            totalQueries: prev.totalQueries + 1,
            totalTokens: prev.totalTokens + (sse.meta?.tokens_used ?? 0),
          }));
        }
      } else if (sse.status === "aborted" && !doneRef.current[sid]) {
        doneRef.current[sid] = true;
        if (sse.tokens) {
          setMessagesMap(prev => {
            const msgs = prev[sid] || [];
            const abortedMsg: ChatMessage = {
              id: `a_${msgCounter.current++}`, role: "assistant",
              content: sse.tokens + "\n\n*(已停止生成)*",
            };
            return { ...prev, [sid]: [...msgs, abortedMsg] };
          });
        }
        setSseStateMap(prev => ({ ...prev, [sid]: DEFAULT_SSE_STATE }));
      } else if (sse.status === "error" && !doneRef.current[sid]) {
        doneRef.current[sid] = true;
        if (sid === sessionId) {
          const isInspectionError = sse.error?.includes("内容安全审核") || sse.error?.includes("inappropriate content");
          const errorMsg: ChatMessage = { 
            id: `e_${msgCounter.current++}`, role: "assistant", 
            content: isInspectionError ? "🚫 **会话历史触发了内容审核**。\n\n请点击左侧的 **「+ 新会话」**。" : `❌ 错误: ${sse.error}` 
          };
          setMessagesMap(prev => ({ ...prev, [sid]: [...(prev[sid] || []), errorMsg] }));
        }
        setSseStateMap(prev => ({ ...prev, [sid]: DEFAULT_SSE_STATE }));
      } else if (sse.status === "idle" || sse.status === "connecting" || sse.status === "streaming") {
        doneRef.current[sid] = false;
      }
    });
  }, [sseStateMap, sessionId]);

  return (
    <ChatContext.Provider
      value={{
        messages: currentMessages, 
        sessionId, 
        userId, 
        settings, 
        stats, 
        sessions, 
        sessionsLoading, 
        // 将当前会话的 SSE 状态 + streamSessionId 提供给组件
        sse: { ...currentSseState, streamSessionId: sessionId },
        generatingSessionIds, // ★ 将后台并发会话列表传递给外部
        sendMessage, stopGeneration, clearHistory, newSession, updateSettings, refreshSessions, switchSession, removeSession,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

// ==========================================
// 独立的事件处理函数，把数据更新进对应的 Map
// ==========================================
function handleSSEEvent(
  sid: string, 
  eventType: string, 
  data: any, 
  setSseStateMap: React.Dispatch<React.SetStateAction<Record<string, SessionSSEState>>>
) {
  setSseStateMap(prev => {
    const state = prev[sid] || DEFAULT_SSE_STATE;
    switch (eventType) {
      case "progress":
        return { ...prev, [sid]: { ...state, progressMessage: data.message } };
      case "token":
        return { ...prev, [sid]: { ...state, tokens: state.tokens + data.text } };
      case "sources":
        return { ...prev, [sid]: { ...state, sources: data.sources } };
      case "done":
        return { ...prev, [sid]: { ...state, meta: data } };
      case "error":
        return { ...prev, [sid]: { ...state, status: "error", error: data.message } };
      case "revise":
        return { ...prev, [sid]: { ...state, tokens: data.text } };
      case "reset_tokens":
        return { ...prev, [sid]: { ...state, tokens: "" } };
      default:
        return prev;
    }
  });
}