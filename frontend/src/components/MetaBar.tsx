"use client";

// 智能搜索助手 — 元信息栏 (置信度 / 延迟 / Token / 降级)
import type { DoneEvent } from "../lib/types";

export default function MetaBar({ meta }: { meta: DoneEvent }) {
  const confidencePct = `${Math.round(meta.confidence * 100)}%`;

  return (
    <p className="text-xs text-gray-400 mt-1.5">
      置信度 {confidencePct} · {meta.latency_ms.toFixed(0)}ms · {meta.tokens_used} tokens
      {meta.is_fallback && (
        <span className="ml-2 px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded text-[10px] font-medium">
          降级回答
        </span>
      )}
    </p>
  );
}
