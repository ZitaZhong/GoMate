// components/itinerary/ModifyInput.tsx
// AI 自然语言修改输入（DD-19 §3.7 / PRD §7.8.1）：
// 底部固定（sticky bottom）+ 七条快捷指令（lib/constants.ts QUICK_COMMANDS）+
// 支持 VerticalTimeline 节点操作预填（如"替换 参观展览"）。
"use client";

import { useState } from "react";
import { QUICK_COMMANDS } from "@/lib/constants";

export interface ModifyInputProps {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  /** 由 VerticalTimeline 节点操作预填 */
  prefill?: string;
}

export function ModifyInput({ onSubmit, disabled, prefill }: ModifyInputProps) {
  const [text, setText] = useState(prefill ?? "");
  // 节点操作预填：渲染期派生状态（React 推荐模式，替代 effect 内同步 setState）
  const [lastPrefill, setLastPrefill] = useState(prefill);
  if (prefill !== lastPrefill) {
    setLastPrefill(prefill);
    if (prefill) setText(prefill);
  }

  const send = (value: string) => {
    const v = value.trim();
    if (!v || disabled) return;
    onSubmit(v);
    setText("");
  };

  return (
    <div className="sticky bottom-0 bg-card border-t border-border p-3 space-y-2">
      {/* 快捷指令（PRD §7.8.1 七条） */}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {QUICK_COMMANDS.map((cmd) => (
          <button
            key={cmd}
            onClick={() => send(cmd)}
            disabled={disabled}
            className="shrink-0 min-h-[36px] text-xs px-3 py-1.5 rounded-badge border border-border
                       text-secondary hover:bg-background disabled:opacity-50"
          >
            {cmd}
          </button>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send(text)}
          placeholder="告诉 GoMate，你想怎么调整？"
          aria-label="修改行程"
          className="flex-1 min-h-[44px] px-3 py-2 border border-border rounded-lg text-sm
                     focus:outline-none focus:ring-2 focus:ring-accent-green/50"
        />
        <button
          onClick={() => send(text)}
          disabled={disabled || !text.trim()}
          className="min-h-[44px] px-4 py-2 bg-primary text-white rounded-lg text-sm
                     disabled:opacity-50 hover:bg-primary/90 transition"
        >
          修改
        </button>
      </div>
    </div>
  );
}
