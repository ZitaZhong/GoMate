// components/chat/MessageBubble.tsx
// 对话气泡（DD-19 §3.2）：user 右对齐、assistant 左对齐卡片；
// 卡片负载经 CardRouter 渲染，clarify 选项渲染为可点 chip（≥44px）。
"use client";

import type { ChatMessage } from "@/lib/types";
import { CardRouter } from "@/components/cards/CardRouter";
import { Tag } from "@/components/ui/Tag";
import { StreamingText } from "./StreamingText";

export interface MessageBubbleProps {
  message: ChatMessage;
  /** 当前 planId（卡片内「去回填」入口用；'new' 时不渲染入口） */
  planId?: string;
  /** clarify 选项点选：直接作为用户消息发送（DD-19 §4.2） */
  onOptionClick?: (option: string) => void;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export function MessageBubble({ message, planId, onOptionClick }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] sm:max-w-[75%] rounded-card px-3 py-2 text-sm leading-relaxed
          ${
            isUser
              ? "bg-accent-green/20 text-primary"
              : "bg-card border border-border text-primary"
          }`}
      >
        {message.content &&
          (isUser ? (
            <span className="whitespace-pre-wrap">{message.content}</span>
          ) : (
            <StreamingText text={message.content} active={Boolean(message.streaming)} />
          ))}

        {message.options && message.options.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {message.options.map((opt) => (
              <Tag key={opt} color="blue" onClick={() => onOptionClick?.(opt)}>
                {opt}
              </Tag>
            ))}
          </div>
        )}

        {message.cards && message.cards.length > 0 && (
          <div className="mt-2 space-y-2">
            {message.cards.map((card, i) => (
              <CardRouter key={i} payload={card} planId={planId} />
            ))}
          </div>
        )}

        <div className={`mt-1 text-[11px] ${isUser ? "text-right" : ""} text-secondary/70`}>
          {fmtTime(message.timestamp)}
        </div>
      </div>
    </div>
  );
}
