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

  useEffect(() => {
    if (!isStreaming) {
      setShowCursor(false);
      return;
    }
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
