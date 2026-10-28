"use client";

import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import type { ChatMessage, UserSettings, SessionSummary, ChatRequest, SourceInfo, DoneEvent } from "../lib/types";
import { generateSessionId, getOrCreateUserId, getSession, deleteSession as apiDeleteSession } from "../lib/api";

const SSE_BASE = process.env.NEXT_PUBLIC_BACKEND_URL ?? "";
const LOCAL_SESSIONS_KEY = "qa_local_sessions_v1";
const THEME_KEY = "qa_theme"; // ★ 新增：主题存储 Key

export interface SessionSSEState {
  status: "idle" | "connecting" | "streaming" | "done" | "error" | "aborted";
  tokens: string;
  sources: SourceInfo[];
  meta: DoneEvent | null;
  error: string | null;
  progressMessage: string | null;
  reasoningMessage: string | null; 
}

const DEFAULT_SSE_STATE: SessionSSEState = {
  status: "idle", tokens: "", sources: [], meta: null, error: null, progressMessage: null, reasoningMessage: null,
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
  messages: ChatMessage[]; 
  sessionId: string;
  userId: string;
  settings: UserSettings;
  stats: { totalQueries: number; totalTokens: number };
  sessions: SessionSummary[];
  sessionsLoading: boolean;
  sse: SessionSSEState & { streamSessionId: string };
  generatingSessionIds: string[];
  theme: "light" | "dark"; // ★ 新增主题
  toggleTheme: () => void; // ★ 新增切换主题方法
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
  
  const [messagesMap, setMessagesMap] = useState<Record<string, ChatMessage[]>>({});
  const [sseStateMap, setSseStateMap] = useState<Record<string, SessionSSEState>>({});
  
  const [settings, setSettings] = useState<UserSettings>(defaultSettings);
  const [stats, setStats] = useState({ totalQueries: 0, totalTokens: 0 });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // ★ 新增：主题状态
  const [theme, setTheme] = useState<"light" | "dark">("light");

  const abortControllers = useRef<Record<string, AbortController>>({});
  const msgCounter = useRef(0);

  const currentMessages = messagesMap[sessionId] || [];
  const currentSseState = sseStateMap[sessionId] || DEFAULT_SSE_STATE;

  const generatingSessionIds = Object.keys(sseStateMap).filter(
    (sid) => sseStateMap[sid].status === "connecting" || sseStateMap[sid].status === "streaming"
  );

  // ★ 初始化：挂载时读取 Theme
  useEffect(() => {
    setUserId(getOrCreateUserId());
    setSessions(getLocalSessions());

    const savedTheme = localStorage.getItem(THEME_KEY) as "light" | "dark" | null;
    const initialTheme = savedTheme || "light";
    setTheme(initialTheme);
    
    if (initialTheme === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  }, []);

  // ★ 新增：切换 Theme 的逻辑
  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === "light" ? "dark" : "light";
      localStorage.setItem(THEME_KEY, next);
      if (next === "dark") {
        document.documentElement.classList.add("dark");
      } else {
        document.documentElement.classList.remove("dark");
      }
      return next;
    });
  }, []);

  const refreshSessions = useCallback(async () => {
    setSessions(getLocalSessions());
  }, []);

  const switchSession = useCallback(
    async (sid: string) => {
      if (!userId || sid === sessionId) return;
      
      setSessionId(sid);
      
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

      if (abortControllers.current[sid]) {
        abortControllers.current[sid].abort();
      }

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

      const userMsg: ChatMessage = { id: `u_${msgCounter.current++}`, role: "user", content };
      setMessagesMap(prev => ({ ...prev, [sid]: [...(prev[sid] || []), userMsg] }));

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
                  handleSSEEvent(sid, currentEvent, data, setSseStateMap);
                } catch {}
              }
              currentEvent = "";
            }
          }
        }

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

  const doneRef = useRef<Record<string, boolean>>({});
  useEffect(() => {
    Object.keys(sseStateMap).forEach(sid => {
      const sse = sseStateMap[sid];
      
      if (sse.status === "done" && !doneRef.current[sid]) {
        doneRef.current[sid] = true;
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
          const isQuotaError = sse.error?.includes("Free quota exhausted") || sse.error?.includes("insufficient_quota");
          
          let errText = `❌ 错误: ${sse.error}`;
          if (isInspectionError) {
            errText = "🚫 **会话历史触发了内容审核**。\n\n请点击左侧的 **「+ 新会话」**。";
          } else if (isQuotaError) {
            errText = "💳 **API 免费额度已耗尽**。\n\n请前往控制台充值，或在设置中切换其他大模型。";
          }

          const finalContent = sse.tokens 
            ? sse.tokens + `\n\n*(⚠️ 生成中断: ${errText})*` 
            : errText;

          const errorMsg: ChatMessage = { 
            id: `e_${msgCounter.current++}`, 
            role: "assistant", 
            content: finalContent 
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
        sse: { ...currentSseState, streamSessionId: sessionId },
        generatingSessionIds, 
        theme, // ★
        toggleTheme, // ★
        sendMessage, stopGeneration, clearHistory, newSession, updateSettings, refreshSessions, switchSession, removeSession,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

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
        return { ...prev, [sid]: { ...state, progressMessage: data.message, reasoningMessage: null } };
      case "token":
        return { ...prev, [sid]: { ...state, tokens: state.tokens + data.text, reasoningMessage: null } };
      case "reasoning":
        return { ...prev, [sid]: { ...state, reasoningMessage: data.text.slice(-40) } };
      case "heartbeat":
        return prev; // 心跳包，忽略即可
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