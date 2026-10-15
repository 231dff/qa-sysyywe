// 智能搜索助手 — API 客户端
import type {
  SessionInfo,
  ConfigDefaults,
  SessionListResponse,
} from "./types";

const BASE = ""; // 同域，通过 Next.js API Route 代理或 rewrites

/** GET /api/sessions?user_id=xxx — 获取用户的会话列表 */
export async function getSessions(userId: string): Promise<SessionListResponse> {
  if (!userId) return { user_id: "", sessions: [] };
  const res = await fetch(
    `${BASE}/api/sessions?user_id=${encodeURIComponent(userId)}`
  );
  if (!res.ok) throw new Error(`获取会话列表失败: ${res.status}`);
  return res.json();
}

/** GET /api/session/:id?user_id=xxx — 获取会话历史 */
export async function getSession(
  sessionId: string,
  userId?: string
): Promise<SessionInfo> {
  const qs = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
  const res = await fetch(`${BASE}/api/session/${sessionId}${qs}`);
  if (!res.ok) throw new Error(`获取会话失败: ${res.status}`);
  return res.json();
}

/** DELETE /api/session/:id?user_id=xxx — 清除会话 */
export async function deleteSession(
  sessionId: string,
  userId?: string
): Promise<void> {
  const qs = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
  const res = await fetch(`${BASE}/api/session/${sessionId}${qs}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`删除会话失败: ${res.status}`);
}

/** GET /api/config/defaults — 获取默认配置 */
export async function getConfigDefaults(): Promise<ConfigDefaults> {
  const res = await fetch(`${BASE}/api/config/defaults`);
  if (!res.ok) throw new Error(`获取配置失败: ${res.status}`);
  return res.json();
}

/** 生成随机会话 ID */
export function generateSessionId(): string {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

const USER_ID_KEY = "search_agent_user_id";

/** 获取或创建持久化用户 ID */
export function getOrCreateUserId(): string {
  if (typeof window === "undefined") return "";
  try {
    let id = window.localStorage.getItem(USER_ID_KEY);
    if (!id) {
      // 兼容非 HTTPS 环境（crypto.randomUUID 在 http://qa.local 可能不可用）
      if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        id = `u_${crypto.randomUUID()}`;
      } else {
        id = `u_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
      }
      window.localStorage.setItem(USER_ID_KEY, id);
    }
    return id;
  } catch {
    return "";
  }
}