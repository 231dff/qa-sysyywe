"use client";

import React, { useState } from "react";
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

  // ★ 解析文本中的 <artifact> 标签
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

    // ★ 核心修改：把大模型吐出的 [低置信度] 标签转换成醒目的 Markdown 标记
    // 用 ⚠️ 和加粗让它在 prose 中极其显眼（免去引入新库的麻烦）
    cleanText = cleanText.replace(
      /\[低置信度\]/g, 
      ' **<span style="color: #b45309; background-color: #fef3c7; padding: 2px 6px; border-radius: 4px; font-size: 12px; border: 1px solid #fde68a;">⚠️ 低置信度</span>** '
    );

    return { cleanText, charts };
  };

  const { cleanText, charts } = parseContent(msg.content);

  return (
    <div className={`flex w-full mb-6 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex flex-col ${isUser ? "items-end max-w-[85%]" : "items-start w-full"}`}>
        
        {/* 消息主体 */}
        {isUser ? (
          <div className="bg-[#f4f4f4] text-[#1a1a1a] px-5 py-3 rounded-2xl rounded-tr-sm text-[15px] leading-relaxed shadow-sm">
            {msg.content}
          </div>
        ) : (
          <div className="w-full text-[#1a1a1a] text-[15px] leading-[1.8]">
            <div className="prose prose-gray max-w-none prose-p:my-2 prose-pre:bg-[#1e1e1e] prose-pre:text-gray-100">
              {/* ★ 注意：如果你想彻底解析 HTML 标签，需要 react-markdown 允许 raw html
                  如果不允许，上面的替换会把完整的 HTML 打印出来。
                  为了不引入 rehype-raw，我们这里直接替换成纯文本+表情：
                  
                  如果上面的方案显示异常，请把 parseContent 里的正则替换改为：
                  cleanText = cleanText.replace(/\[低置信度\]/g, ' **[⚠️ 低置信度]** ');
              */}
              <ReactMarkdown 
                remarkPlugins={[remarkGfm]}
                // 如果没装 rehype-raw，请去掉下面这行，并把上面替换逻辑改为纯 Markdown 加粗
                // rehypePlugins={[rehypeRaw]} 
              >
                {cleanText}
              </ReactMarkdown>
            </div>

            {/* ★ 渲染图表 */}
            {charts.length > 0 && (
              <div className="mt-4 flex flex-col gap-4 w-full">
                {charts.map((config, idx) => (
                  <div key={idx} className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm w-full">
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
              <span className="inline-block w-2 h-4 bg-gray-800 animate-pulse ml-1 align-middle" />
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