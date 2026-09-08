"use client";

// 智能搜索助手 — 流式打字效果文本
import { useEffect, useState } from "react";

export default function StreamingToken({
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
    <span>
      {text}
      {showCursor && isStreaming && (
        <span className="inline-block w-px h-4 bg-gray-400 ml-0.5" />
      )}
    </span>
  );
}
