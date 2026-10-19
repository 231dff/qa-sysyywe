"use client";

import { useState, useRef } from "react";

interface ChatInputProps {
  onSubmit: (text: string) => void;
  disabled: boolean;
}

export default function ChatInput({ onSubmit, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSubmit(text);
    setValue("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
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
    <div className="relative flex items-end w-full bg-white border border-gray-300 rounded-[24px] shadow-sm px-3 py-2 focus-within:border-gray-400 focus-within:ring-1 focus-within:ring-gray-400 transition-all">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        placeholder="给智能搜索助手发送消息..."
        disabled={disabled}
        rows={1}
        className="w-full bg-transparent px-2 py-2 text-[15px] text-gray-800 resize-none focus:outline-none max-h-[200px] min-h-[44px]"
      />
      <button
        onClick={handleSubmit}
        disabled={disabled || !value.trim()}
        className="mb-1 mr-1 flex items-center justify-center w-8 h-8 rounded-full bg-black hover:bg-gray-800 text-white transition disabled:opacity-30 disabled:cursor-not-allowed shrink-0"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"></line>
          <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
        </svg>
      </button>
    </div>
  );
}