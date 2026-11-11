"use client";

import React, { useState, useEffect, useRef, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../lib/types";
import ReactECharts from "echarts-for-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { submitFeedback } from "../lib/api";

// ★ 修复：替换 any 为具体的类型
function CodeBlock({ language, code, ...props }: { language: string; code: string; [key: string]: unknown }) {
  const [isCopied, setIsCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setIsCopied(true);
      setTimeout(() => setIsCopied(false), 2000);
    } catch {
      // ★ 修复：去掉未使用的 err 变量
    }
  };
  const handleDownload = () => {
    const blob = new Blob([code], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `snippet.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };
  return (
    <div className="my-4 rounded-xl overflow-hidden border border-gray-200 dark:border-[#3e3e42] shadow-sm bg-[#282c34]">
      <div className="flex justify-between items-center px-4 py-1.5 bg-[#21252b] text-xs text-gray-400 border-b border-[#181a1f]">
        <span className="font-mono uppercase">{language || "text"}</span>
        <div className="flex items-center gap-3">
          <button onClick={handleCopy} className="flex items-center gap-1 hover:text-white transition-colors">
            {isCopied ? <span className="text-[#10b981]">已复制</span> : <span>复制</span>}
          </button>
          <button onClick={handleDownload} className="flex items-center gap-1 hover:text-white transition-colors">下载</button>
        </div>
      </div>
      <SyntaxHighlighter style={oneDark as unknown as Record<string, React.CSSProperties>} language={language} PreTag="div" customStyle={{ margin: 0, padding: '1rem', background: '#282c34', fontSize: '14px' }} {...props}>
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

export default function MessageBubble({
  msg,
  isStreaming,
  sessionId,
}: {
  msg: ChatMessage;
  isStreaming: boolean;
  sessionId?: string; 
}) {
  const isUser = msg.role === "user";
  const [isExpanded, setIsExpanded] = useState<boolean>(msg.isThinking || false);
  const reasoningRef = useRef<HTMLDivElement>(null);
  const hasAutoFolded = useRef(false);

  const fallbackId = useMemo(() => {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return `m_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  }, []);
  
  // ★ 修复：移除 as any
  const messageId: string = msg.message_id || msg.id || fallbackId;

  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    // ★ 修复：移除 as any
    const existing = msg.feedback;
    if (existing) setFeedback(existing);
  }, [msg]);

  const handleFeedback = async (kind: "up" | "down") => {
    if (submitting) return;
    if (!sessionId) {
      console.warn("[feedback] 缺少 sessionId，无法提交");
      return;
    }
    const previous = feedback;
    setFeedback(kind);            
    setSubmitting(true);
    try {
      const res = await submitFeedback({
        session_id: sessionId,
        message_id: messageId,
        feedback: kind,
        query: msg.query || "", // ★ 修复：移除 as any
        answer_preview: (msg.content || "").slice(0, 200),
        trace_id: msg.meta?.trace_id || "", // ★ 修复：移除 as any
        user_id: (() => {
          try {
            return window.localStorage.getItem("search_agent_user_id") || "";
          } catch {
            return "";
          }
        })(),
      });
      if (!res?.ok) throw new Error("提交失败");
    } catch (e) {
      console.error("[feedback] 提交失败", e);
      setFeedback(previous);      
    } finally {
      setSubmitting(false);
    }
  };

  useEffect(() => {
    if (msg.isThinking) {
      setIsExpanded(true);
      hasAutoFolded.current = false;
    } else if (!msg.isThinking && !hasAutoFolded.current) {
      setIsExpanded(false);
      hasAutoFolded.current = true;
    }
  }, [msg.isThinking]);

  useEffect(() => {
    if (msg.isThinking && isExpanded && reasoningRef.current) {
      reasoningRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [msg.reasoningText, msg.actions, msg.isThinking, isExpanded]);

  const parseContent = (text: string) => {
    let cleanText = text;
    const charts: Record<string, unknown>[] = []; // ★ 修复：any[] 改为 Record<string, unknown>[]
    const artifactRegex = /<artifact>([\s\S]*?)<\/artifact>/g;
    const matches = [...text.matchAll(artifactRegex)];

    matches.forEach((match) => {
      try {
        const chartConfig = JSON.parse(match[1].trim());
        charts.push(chartConfig);
        cleanText = cleanText.replace(match[0], "");
      } catch {
        // ★ 修复：去掉未使用的 e
        cleanText = cleanText.replace(match[0], "\n*(图表配置解析失败)*\n");
      }
    });

    const unclosedIndex = cleanText.indexOf("<artifact>");
    if (unclosedIndex !== -1) {
      cleanText = cleanText.slice(0, unclosedIndex) + "\n\n> 📊 *正在绘制图表...*";
    }

    cleanText = cleanText.replace(/\[低置信度\]/g, ' **[⚠️ 低置信度]** ');
    return { cleanText, charts };
  };

  const { cleanText, charts } = parseContent(msg.content);
  const showThinking = !isUser && (msg.isThinking || msg.reasoningText || msg.actions?.length || msg.progressMessage);

  return (
    <div className={`flex w-full mb-6 ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex flex-col ${isUser ? "items-end max-w-[85%]" : "items-start w-full"}`}>
        
        {isUser ? (
          <div className="bg-[#f4f4f4] dark:bg-[#2b3a50] text-[#1a1a1a] dark:text-[#e0e0e0] px-5 py-3 rounded-2xl rounded-tr-sm text-[16px] leading-[1.8] shadow-sm transition-colors">
            {msg.content}
          </div>
        ) : (
          <div className="w-full text-[#1a1a1a] dark:text-[#e0e0e0] text-[16px] leading-[1.9]">
            
            {showThinking && (
              <div className="mb-5 flex flex-col gap-2">
                <button
                  onClick={() => setIsExpanded(!isExpanded)}
                  className="flex items-center gap-2 w-fit text-[15px] text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200 transition-colors select-none"
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
                    {msg.isThinking ? "正在思考..." : `已思考 (用时 ${msg.reasoningDuration || 0} 秒)`}
                  </span>
                  <svg className={`w-4 h-4 transition-transform duration-300 ${isExpanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                
                {isExpanded && (
                  <div className="relative mt-1">
                    <div className="absolute left-[8px] top-2 bottom-2 w-[1.5px] bg-gray-200 dark:bg-gray-700 rounded-full" />
                    
                    <div 
                      ref={reasoningRef}
                      className="pl-6 pr-4 text-[15px] text-gray-500 dark:text-gray-400 leading-[1.8] max-h-[400px] overflow-y-auto custom-scrollbar scroll-smooth"
                    >
                      {msg.actions && msg.actions.length > 0 && (
                        <div className="mb-4 space-y-2">
                          {msg.actions.map((action, idx) => (
                            <div key={idx} className="flex items-start gap-2 text-[14px]">
                              <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-blue-400/80 shrink-0" />
                              <span className="text-gray-400 dark:text-gray-500">{action}</span>
                            </div>
                          ))}
                        </div>
                      )}

                      <div className="prose prose-sm dark:prose-invert max-w-none 
                        prose-p:text-[15px] prose-p:my-1.5 prose-p:leading-[1.8]
                        prose-ul:text-[15px] prose-ul:my-1.5 prose-ul:pl-4
                        prose-ol:text-[15px] prose-ol:my-1.5 prose-ol:pl-4
                        prose-li:my-0.5
                        prose-blockquote:border-l-0 prose-blockquote:pl-0 prose-blockquote:my-0 prose-blockquote:italic prose-blockquote:text-gray-500
                        prose-strong:text-gray-700 dark:prose-strong:text-gray-300
                        prose-code:text-pink-600 dark:prose-code:text-pink-400 prose-code:bg-gray-100 dark:prose-code:bg-gray-800 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:before:content-none prose-code:after:content-none prose-code:font-normal
                      ">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {msg.reasoningText || (!msg.actions?.length ? msg.progressMessage || "正在组织语言..." : "")}
                        </ReactMarkdown>
                        <div className="h-2" />
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {cleanText && (
              <div className="prose prose-sm dark:prose-invert max-w-none 
                prose-p:text-[16px] prose-p:my-2 prose-p:leading-[1.9]
                prose-h1:text-2xl prose-h1:mb-4 prose-h1:font-semibold
                prose-h2:text-xl prose-h2:mb-3 prose-h2:font-semibold
                prose-h3:text-lg prose-h3:mb-2 prose-h3:font-semibold
                prose-ul:text-[16px] prose-ul:my-2 prose-ul:pl-5
                prose-ol:text-[16px] prose-ol:my-2 prose-ol:pl-5
                prose-li:my-1
                prose-pre:p-0 prose-pre:bg-transparent
                prose-a:text-blue-500 dark:prose-a:text-blue-400 prose-a:no-underline hover:prose-a:underline
              ">
                {isStreaming ? (
                  <div className="whitespace-pre-wrap font-sans">{cleanText}</div>
                ) : (
                  <ReactMarkdown 
                    remarkPlugins={[remarkGfm]}
                    components={{
                      // ★ 修复：替换 any 为具体的类型
                      code({ inline, className, children, node: _node, ...props }: React.HTMLAttributes<HTMLElement> & { inline?: boolean; node?: unknown }) {
                        const match = /language-(\w+)/.exec(className || '');
                        const codeString = String(children).replace(/\n$/, '');
                        return !inline && match ? (
                          <CodeBlock language={match[1]} code={codeString} {...props} />
                        ) : (
                          <code className="bg-gray-100 dark:bg-[#2a2a2d] text-pink-600 dark:text-pink-400 px-1.5 py-0.5 rounded text-[15px] font-mono" {...props}>
                            {children}
                          </code>
                        );
                      }
                    }}
                  >
                    {cleanText}
                  </ReactMarkdown>
                )}
              </div>
            )}

            {charts.length > 0 && !isStreaming && (
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

        {/* 来源与 Meta 信息 + 反馈按钮 */}
        {!isUser && !isStreaming && (
          <div className="mt-3 flex flex-col gap-2 w-full">
            {msg.sources && msg.sources.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {msg.sources.map((s, idx) => (
                  <a key={idx} href={s.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-white dark:bg-[#2a2a2d] border border-gray-200 dark:border-gray-700 text-[14px] text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-[#333] transition">
                    <span className="text-blue-500 dark:text-blue-400 font-bold">{idx + 1}</span>
                    <span className="truncate max-w-[200px]">{s.title || s.url}</span>
                  </a>
                ))}
              </div>
            )}
            
            <div className="flex items-center justify-between gap-3">
              {msg.meta && (
                <div className="text-[13px] text-gray-400 dark:text-gray-500 flex items-center gap-3">
                  <span>置信度 {Math.round(msg.meta.confidence * 100)}%</span>
                  <span>·</span>
                  <span>耗时 {msg.meta.latency_ms.toFixed(0)}ms</span>
                  <span>·</span>
                  <span>{msg.meta.tokens_used} tokens</span>
                </div>
              )}

              <div className="flex items-center gap-1 ml-auto">
                <button
                  onClick={() => handleFeedback("up")}
                  disabled={submitting}
                  title="有帮助"
                  aria-label="有帮助"
                  className={`p-1.5 rounded-md transition-colors ${
                    feedback === "up"
                      ? "bg-green-100 dark:bg-green-900/40 text-green-600 dark:text-green-400"
                      : "text-gray-400 hover:text-green-600 dark:hover:text-green-400 hover:bg-gray-100 dark:hover:bg-[#333]"
                  } ${submitting ? "opacity-50 cursor-not-allowed" : ""}`}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill={feedback === "up" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2h0a3.13 3.13 0 0 1 3 3.88Z"/>
                  </svg>
                </button>
                <button
                  onClick={() => handleFeedback("down")}
                  disabled={submitting}
                  title="没帮助"
                  aria-label="没帮助"
                  className={`p-1.5 rounded-md transition-colors ${
                    feedback === "down"
                      ? "bg-red-100 dark:bg-red-900/40 text-red-600 dark:text-red-400"
                      : "text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-[#333]"
                  } ${submitting ? "opacity-50 cursor-not-allowed" : ""}`}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill={feedback === "down" ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22h0a3.13 3.13 0 0 1-3-3.88Z"/>
                  </svg>
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}