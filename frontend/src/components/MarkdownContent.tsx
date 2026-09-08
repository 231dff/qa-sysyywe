"use client";

// 智能搜索助手 — Markdown 内容渲染 + 流式光标
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MarkdownContent({
  text,
  isStreaming,
}: {
  text: string;
  isStreaming: boolean;
}) {
  const [showCursor, setShowCursor] = useState(true);

  // 仅负责流式期间的光标闪烁；是否显示光标由渲染处的 `isStreaming` 守卫，
  // 无需在 effect 内同步 setState（React 19 反模式，会触发级联渲染）。
  useEffect(() => {
    if (!isStreaming) return;
    const timer = setInterval(() => {
      setShowCursor((v) => !v);
    }, 500);
    return () => clearInterval(timer);
  }, [isStreaming]);

  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {text}
      </ReactMarkdown>
      {showCursor && isStreaming && text && (
        <span className="inline-block w-px h-4 bg-gray-400 ml-0.5 align-middle" />
      )}
    </div>
  );
}
