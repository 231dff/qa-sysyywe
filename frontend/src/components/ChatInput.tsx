"use client";

import { useState, useRef } from "react";

interface ChatInputProps {
  // ★ 修改：回调带上开关状态
  onSubmit: (text: string, options: { deepThink: boolean; webSearch: boolean }) => void;
  onStop: () => void;
  disabled: boolean;
}

export default function ChatInput({ onSubmit, onStop, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [deepThink, setDeepThink] = useState(false);
  const [webSearch, setWebSearch] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    // ★ 核心修改：提交时附带状态
    onSubmit(text, { deepThink, webSearch });
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  };

  return (
    <div className="relative flex flex-col w-full bg-white dark:bg-[#2a2a2d] border border-gray-300 dark:border-[#3e3e42] rounded-[24px] shadow-sm px-4 pt-3 pb-3 focus-within:border-gray-400 dark:focus-within:border-gray-500 transition-all">
      
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        placeholder="给智能搜索助手发送消息..."
        disabled={disabled}
        rows={1}
        className="w-full bg-transparent px-1 py-1 text-[15px] text-gray-800 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 resize-none focus:outline-none max-h-[200px] min-h-[44px]"
      />

      <div className="flex justify-between items-center mt-2">
        
        <div className="flex items-center gap-2">
          {/* 深度思考 */}
          <button
            onClick={() => setDeepThink(!deepThink)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[13px] font-medium transition-colors border ${
              deepThink 
                ? "bg-blue-50 border-blue-200 text-blue-600 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-400" 
                : "bg-transparent border-gray-200 text-gray-500 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-[#333]"
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>
            </svg>
            深度思考
          </button>

          {/* 联网搜索 */}
          <button
            onClick={() => setWebSearch(!webSearch)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[13px] font-medium transition-colors border ${
              webSearch 
                ? "bg-blue-50 border-blue-200 text-blue-600 dark:bg-blue-900/20 dark:border-blue-800 dark:text-blue-400" 
                : "bg-transparent border-gray-200 text-gray-500 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-[#333]"
            }`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            智能搜索
          </button>
        </div>

        {disabled ? (
          <button
            onClick={onStop}
            className="flex items-center justify-center w-8 h-8 rounded-full bg-gray-800 hover:bg-black dark:bg-gray-200 dark:hover:bg-white text-white dark:text-gray-900 transition shrink-0"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <rect x="4" y="4" width="16" height="16" rx="2" />
            </svg>
          </button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!value.trim()}
            className="flex items-center justify-center w-8 h-8 rounded-full bg-blue-500 hover:bg-blue-600 text-white transition disabled:opacity-30 disabled:bg-gray-300 dark:disabled:bg-gray-600 disabled:cursor-not-allowed shrink-0"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="19" x2="12" y2="5"></line>
              <polyline points="5 12 12 5 19 12"></polyline>
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}