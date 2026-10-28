"use client";

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types";
import ReactECharts from "echarts-for-react";

export default function MessageBubble({
  msg,
  isStreaming,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
}) {
  const isUser = msg.role === "user";

  const parseContent = (text: string) => {
    const artifactRegex = /<artifact>([\s\S]*?)<\/artifact>/g;
    const matches = [...text.matchAll(artifactRegex)];
    
    let cleanText = text;
    const charts: any[] = [];

    matches.forEach((match) => {
      try {
        const chartConfig = JSON.parse(match[1].trim());
        charts.push(chartConfig);
        cleanText = cleanText.replace(match[0], "");
      } catch (e) {
        console.error("解析 Artifact JSON 失败", e);
      }
    });

    // 把 [低置信度] 标签转换纯文本+表情
    cleanText = cleanText.replace(/\[低置信度\]/g, ' **[⚠️ 低置信度]** ');

    return { cleanText, charts };
  };

  const { cleanText, charts } = parseContent(msg.content);

  return (
    <div className={`flex w-full mb-6 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex flex-col ${isUser ? "items-end max-w-[85%]" : "items-start w-full"}`}>
        
        {/* 消息主体 */}
        {isUser ? (
          <div className="bg-[#f4f4f4] dark:bg-[#2b3a50] text-[#1a1a1a] dark:text-[#e0e0e0] px-5 py-3 rounded-2xl rounded-tr-sm text-[15px] leading-relaxed shadow-sm transition-colors">
            {msg.content}
          </div>
        ) : (
          <div className="w-full text-[#1a1a1a] dark:text-[#e0e0e0] text-[15px] leading-[1.8]">
            <div className="prose prose-gray dark:prose-invert max-w-none prose-p:my-2 prose-pre:bg-[#1e1e1e] prose-pre:text-gray-100 prose-a:text-blue-600 dark:prose-a:text-blue-400">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {cleanText}
              </ReactMarkdown>
            </div>

            {/* 渲染图表 */}
            {charts.length > 0 && (
              <div className="mt-4 flex flex-col gap-4 w-full">
                {charts.map((config, idx) => (
                  <div key={idx} className="bg-white dark:bg-[#2a2a2d] p-4 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm w-full transition-colors">
                    <ReactECharts 
                      option={config} 
                      style={{ height: '350px', width: '100%' }} 
                      notMerge={true}
                    />
                  </div>
                ))}
              </div>
            )}
            
            {/* 光标动画 */}
            {isStreaming && (
              <span className="inline-block w-2 h-4 bg-gray-800 dark:bg-gray-300 animate-pulse ml-1 align-middle" />
            )}
          </div>
        )}

        {/* 来源与 Meta 信息 */}
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
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-white dark:bg-[#2a2a2d] border border-gray-200 dark:border-gray-700 text-[13px] text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-[#333] transition"
                  >
                    <span className="text-blue-500 dark:text-blue-400 font-bold">{idx + 1}</span>
                    <span className="truncate max-w-[200px]">{s.title || s.url}</span>
                  </a>
                ))}
              </div>
            )}
            
            {msg.meta && (
              <div className="text-[12px] text-gray-400 dark:text-gray-500 flex items-center gap-3">
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