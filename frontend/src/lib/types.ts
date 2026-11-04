// 智能搜索助手 — 类型定义

export interface ChatRequest {
  query: string;
  session_id?: string | null;
  user_id?: string | null;
  model?: string | null;
  search_depth?: string | null;
  top_k?: number | null;
  
  provider?: string | null; 
  api_base?: string | null;
  api_key?: string | null;
  mcp_config?: Record<string, any> | null;

  enable_deep_think?: boolean;
  enable_search?: boolean;

  fallback_model?: string | null;
  fallback_api_base?: string | null;
  fallback_api_key?: string | null;
}

export interface ProgressEvent {
  node: "rewrite" | "search" | "generate" | "fallback" | "score" | "reviewer" | "cache";
  message: string;
}

export interface TokenEvent { text: string; }
export interface SourceInfo { url: string; title: string; snippet: string; }
export interface SourcesEvent { sources: SourceInfo[]; }

export interface DoneEvent {
  confidence: number;
  latency_ms: number;
  tokens_used: number;
  is_fallback: boolean;
}

export interface ErrorEvent { message: string; code: string; }

export type SSEEvent =
  | { type: "progress"; data: ProgressEvent }
  | { type: "token"; data: TokenEvent }
  | { type: "sources"; data: SourcesEvent }
  | { type: "done"; data: DoneEvent }
  | { type: "error"; data: ErrorEvent }
  | { type: "revise"; data: { text: string } }
  | { type: "reasoning"; data: { text: string } }
  | { type: "heartbeat"; data: any };

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: SourceInfo[];
  meta?: {
    confidence: number;
    latency_ms: number;
    tokens_used: number;
    is_fallback: boolean;
  };
  reasoningText?: string;
  reasoningDuration?: number;
  isThinking?: boolean;
  progressMessage?: string | null;
  actions?: string[]; // ★ 新增：动作列表
}

export interface SessionInfo {
  session_id: string;
  history: { role: string; content: string }[];
  active: boolean;
}

export interface ConfigDefaults {
  default_provider?: string;
  default_model?: string;
  search_depth: string;
  top_k: number;
}

export interface UserSettings {
  model: string;
  searchDepth: string;
  topK: number;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  preview: string;
  message_count: number;
}

export interface SessionListResponse {
  user_id: string;
  sessions: SessionSummary[];
}