"use client";

import React, { useState, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types";
import ReactECharts from "echarts-for-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";

// ============================================================
// ★ 独立抽离的代码块组件（带复制、下载功能）
// ============================================================
function CodeBlock({ language, code, ...props }: { language: string; code: string; [key: string]: any }) {
  const [isCopied, setIsCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setIsCopied(true);
      setTimeout(() => setIsCopied(false), 2000); // 2秒后恢复
    } catch (err) {
      console.error("复制失败", err);
    }
  };

  const handleDownload = () => {
    const blob = new Blob([code], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    // 根据语言设定后缀名
    const ext = language === "python" ? ".py" : language === "javascript" || language === "js" ? ".js" : language === "typescript" || language === "ts" ? ".ts" : language === "json" ? ".json" : ".txt";
    a.download = `code_snippet${ext}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="my-4 rounded-xl overflow-hidden border border-gray-200 dark:border-[#3e3e42] shadow-sm bg-[#282c34]">
      {/* 顶部工具栏 */}
      <div className="flex justify-between items-center px-4 py-1.5 bg-[#21252b] text-xs text-gray-400 border-b border-[#181a1f]">
        <span className="font-mono uppercase">{language || "text"}</span>
        <div className="flex items-center gap-3">
          {/* 复制按钮 */}
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 hover:text-white transition-colors"
            title="复制代码"
          >
            {isCopied ? (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>
                <span className="text-[#10b981]">已复制</span>
              </>
            ) : (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                <span>复制</span>
              </>
            )}
          </button>

          {/* 下载按钮 */}
          <button
            onClick={handleDownload}
            className="flex items-center gap-1 hover:text-white transition-colors"
            title="下载代码"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            <span>下载</span>
          </button>
        </div>
      </div>
      
      {/* 代码高亮区 */}
      <SyntaxHighlighter
        style={oneDark as any}
        language={language}
        PreTag="div"
        customStyle={{ margin: 0, padding: '1rem', background: '#282c34', fontSize: '13px' }}
        {...props}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

// ============================================================
// 主消息气泡组件
// ============================================================
export default function MessageBubble({
  msg,
  isStreaming,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
}) {
  const isUser = msg.role === "user";
  
  const [isExpanded, setIsExpanded] = useState<boolean>(msg.isThinking || false);
  const reasoningRef = useRef<HTMLDivElement>(null);
  const hasAutoFolded = useRef(false);

  // 自动展开、自动折叠逻辑
  useEffect(() => {
    if (msg.isThinking) {
      setIsExpanded(true);
      hasAutoFolded.current = false; 
    } else if (!msg.isThinking && !hasAutoFolded.current) {
      setIsExpanded(false);
      hasAutoFolded.current = true;
    }
  }, [msg.isThinking]);

  // 自动滚动逻辑
  useEffect(() => {
    if (msg.isThinking && isExpanded && reasoningRef.current) {
      reasoningRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [msg.reasoningText, msg.isThinking, isExpanded]);

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
      } catch (e) {}
    });

    cleanText = cleanText.replace(/\[低置信度\]/g, ' **[⚠️ 低置信度]** ');

    return { cleanText, charts };
  };

  const { cleanText, charts } = parseContent(msg.content);
  const showThinking = !isUser && (msg.isThinking || msg.reasoningText || msg.progressMessage);

  return (
    <div className={`flex w-full mb-6 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex flex-col ${isUser ? "items-end max-w-[85%]" : "items-start w-full"}`}>
        
        {isUser ? (
          <div className="bg-[#f4f4f4] dark:bg-[#2b3a50] text-[#1a1a1a] dark:text-[#e0e0e0] px-5 py-3 rounded-2xl rounded-tr-sm text-[15px] leading-relaxed shadow-sm transition-colors">
            {msg.content}
          </div>
        ) : (
          <div className="w-full text-[#1a1a1a] dark:text-[#e0e0e0] text-[15px] leading-[1.8]">
            
            {/* 思考过程区块 */}
            {showThinking && (
              <div className="mb-4 flex flex-col gap-2">
                <button
                  onClick={() => setIsExpanded(!isExpanded)}
                  className="flex items-center gap-2 w-fit text-[14px] text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200 transition-colors select-none"
                >
                  {msg.isThinking ? (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-pulse">
                      <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
                      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
                    </svg>
                  ) : (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
                      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
                    </svg>
                  )}
                  <span className="font-medium">
                    {msg.isThinking ? "正在思考" : `已思考 (用时 ${msg.reasoningDuration || 0} 秒)`}
                  </span>
                  <svg
                    className={`w-4 h-4 transition-transform duration-300 ${isExpanded ? "rotate-180" : ""}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                
                {isExpanded && (
                  <div className="relative">
                    <div 
                      ref={reasoningRef}
                      className="pl-6 text-[14px] text-gray-500 dark:text-gray-400 leading-relaxed max-h-[300px] overflow-y-auto custom-scrollbar scroll-smooth"
                    >
                      <div className="prose prose-sm dark:prose-invert max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {msg.reasoningText || msg.progressMessage || "正在组织语言..."}
                        </ReactMarkdown>
                        <div className="h-2" />
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 正式回答内容 */}
            {cleanText && (
              <div className="prose prose-sm dark:prose-invert max-w-none 
                prose-p:my-2 prose-p:leading-relaxed
                prose-h1:text-xl prose-h1:mb-3 prose-h1:font-semibold
                prose-h2:text-lg prose-h2:mb-2 prose-h2:font-semibold
                prose-h3:text-base prose-h3:mb-2
                prose-ul:my-2 prose-li:my-1
                prose-pre:p-0 prose-pre:bg-transparent
                prose-a:text-blue-500 dark:prose-a:text-blue-400 prose-a:no-underline hover:prose-a:underline
              ">
                <ReactMarkdown 
                  remarkPlugins={[remarkGfm]}
                  components={{
                    // ★ 核心：拦截 code 渲染，使用带复制下载的 CodeBlock
                    code({ node, inline, className, children, ...props }: any) {
                      const match = /language-(\w+)/.exec(className || '');
                      const codeString = String(children).replace(/\n$/, '');
                      
                      return !inline && match ? (
                        <CodeBlock 
                          language={match[1]} 
                          code={codeString} 
                          {...props} 
                        />
                      ) : (
                        <code className="bg-gray-100 dark:bg-[#2a2a2d] text-pink-600 dark:text-pink-400 px-1.5 py-0.5 rounded text-sm font-mono" {...props}>
                          {children}
                        </code>
                      );
                    }
                  }}
                >
                  {cleanText}
                </ReactMarkdown>
              </div>
            )}

            {/* 渲染图表 */}
            {charts.length > 0 && (
              <div className="mt-4 flex flex-col gap-4 w-full">
                {charts.map((config, idx) => (
                  <div key={idx} className="bg-white dark:bg-[#2a2a2d] p-4 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm w-full transition-colors">
                    <ReactECharts option={config} style={{ height: '350px', width: '100%' }} notMerge={true} />
                  </div>
                ))}
              </div>
            )}
            
            {isStreaming && !msg.isThinking && (
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
                  <a key={idx} href={s.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-white dark:bg-[#2a2a2d] border border-gray-200 dark:border-gray-700 text-[13px] text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-[#333] transition">
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