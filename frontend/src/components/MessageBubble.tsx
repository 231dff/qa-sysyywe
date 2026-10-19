"use client";

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types";

export default function MessageBubble({
  msg,
  isStreaming,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
}) {
  const isUser = msg.role === "user";

  return (
    <div className={`flex w-full mb-6 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex flex-col ${isUser ? "items-end max-w-[85%]" : "items-start w-full"}`}>
        
        {/* 消息主体 */}
        {isUser ? (
          // 用户气泡：浅灰色，大圆角，右侧对齐
          <div className="bg-[#f4f4f4] text-[#1a1a1a] px-5 py-3 rounded-2xl rounded-tr-sm text-[15px] leading-relaxed shadow-sm">
            {msg.content}
          </div>
        ) : (
          // AI 气泡：无背景，纯文本排版，极致阅读体验
          <div className="w-full text-[#1a1a1a] text-[15px] leading-[1.8]">
            <div className="prose prose-gray max-w-none prose-p:my-2 prose-pre:bg-[#1e1e1e] prose-pre:text-gray-100 prose-table:text-[14px]">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {msg.content}
              </ReactMarkdown>
            </div>
            
            {/* 光标动画 */}
            {isStreaming && (
              <span className="inline-block w-2 h-4 bg-gray-800 animate-pulse ml-1 align-middle" />
            )}
          </div>
        )}

        {/* 来源与 Meta 信息 (仅 AI 消息显示) */}
        {!isUser && !isStreaming && (
          <div className="mt-3 flex flex-col gap-2">
            {msg.sources && msg.sources.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {msg.sources.map((s, idx) => (
                  <a
                    key={idx}
                    href={s.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-white border border-gray-200 text-[13px] text-gray-600 hover:bg-gray-50 transition"
                  >
                    <span className="text-blue-500 font-bold">{idx + 1}</span>
                    <span className="truncate max-w-[200px]">{s.title || s.url}</span>
                  </a>
                ))}
              </div>
            )}
            
            {msg.meta && (
              <div className="text-[12px] text-gray-400 flex items-center gap-3">
                <span>置信度 {Math.round(msg.meta.confidence * 100)}%</span>
                <span>·</span>
                <span>耗时 {msg.meta.latency_ms.toFixed(0)}ms</span>
                <span>·</span>
                <span>{msg.meta.tokens_used} tokens</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}