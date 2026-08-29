// components/chat/StreamingText.tsx
// 流式文字逐字 reveal（30ms/字，GoMate PRD §13.5 流式除外条款内的打字节奏）。
// active=false 时立即渲染全文（历史消息/回复气泡不回放动画）。
"use client";

import { useEffect, useState } from "react";

const CHAR_INTERVAL_MS = 30;

export interface StreamingTextProps {
  text: string;
  /** true=流式中逐字 reveal；false=直接渲染全文 */
  active?: boolean;
  className?: string;
}

export function StreamingText({ text, active = false, className = "" }: StreamingTextProps) {
  const [shown, setShown] = useState(active ? "" : text);

  useEffect(() => {
    if (!active || shown.length >= text.length) return;
    const timer = setTimeout(
      () => setShown(text.slice(0, shown.length + 1)),
      CHAR_INTERVAL_MS,
    );
    return () => clearTimeout(timer);
  }, [text, shown, active]);

  // 非流式（历史消息/回复气泡）直接渲染全文；流式中逐字 reveal
  const display = active ? shown : text;
  const catchingUp = active && shown.length < text.length;

  return (
    <span className={`whitespace-pre-wrap ${className}`}>
      {display}
      {catchingUp && (
        <span aria-hidden="true" className="inline-block w-1.5 h-4 align-text-bottom bg-accent-green/70 animate-pulse" />
      )}
    </span>
  );
}
