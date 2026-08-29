// components/chat/QuickCommands.tsx
// 横滑快捷指令条（DD-19 §3.2）：点选填入输入框，不直接发送。
// 交互区域 >= 44px（Tag 可点形态保证）；横向滑动为 chip 条设计内行为。
"use client";

import { Tag } from "@/components/ui/Tag";

/** 跨城规划默认快捷指令（填充输入框后由用户编辑/发送）。 */
export const CHAT_QUICK_COMMANDS = [
  "这个周末想去周边城市",
  "帮我比较高铁和飞机",
  "预算控制在 2000 以内",
  "周五晚出发，周日回",
  "想去看展或演出",
  "改成雨天方案",
] as const;

export interface QuickCommandsProps {
  onSelect: (command: string) => void;
  commands?: readonly string[];
  disabled?: boolean;
}

export function QuickCommands({
  onSelect,
  commands = CHAT_QUICK_COMMANDS,
  disabled = false,
}: QuickCommandsProps) {
  return (
    <div
      className="flex gap-2 overflow-x-auto px-3 py-2 border-t border-border bg-card
                 whitespace-nowrap [-ms-overflow-style:none] [scrollbar-width:none]
                 [&::-webkit-scrollbar]:hidden"
    >
      {commands.map((cmd) => (
        <Tag
          key={cmd}
          color="green"
          disabled={disabled}
          onClick={() => onSelect(cmd)}
          className="shrink-0"
        >
          {cmd}
        </Tag>
      ))}
    </div>
  );
}
