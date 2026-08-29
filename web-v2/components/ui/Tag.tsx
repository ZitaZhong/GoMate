// components/ui/Tag.tsx
// 标签/筛选 chip。可点选（兴趣标签多选）时交互区域 >= 44px；
// 纯展示（如卡片上的小标签）用小号 chip。
import type { ReactNode } from "react";

type TagColor = "neutral" | "green" | "blue" | "yellow" | "coral" | "red";

const COLOR_CLS: Record<TagColor, { base: string; active: string }> = {
  neutral: { base: "bg-black/5 text-secondary", active: "bg-primary text-white" },
  green: { base: "bg-accent-green/15 text-primary", active: "bg-accent-green text-white" },
  blue: { base: "bg-accent-blue/15 text-primary", active: "bg-accent-blue text-white" },
  yellow: { base: "bg-accent-yellow/25 text-primary", active: "bg-accent-yellow text-primary" },
  coral: { base: "bg-accent-coral/15 text-primary", active: "bg-accent-coral text-white" },
  red: { base: "bg-accent-red/15 text-primary", active: "bg-accent-red text-white" },
};

export interface TagProps {
  children: ReactNode;
  color?: TagColor;
  /** 提供 onClick 即渲染为可选中 chip（>= 44px 交互区域） */
  selected?: boolean;
  onClick?: () => void;
  disabled?: boolean;
  className?: string;
}

export function Tag({
  children,
  color = "neutral",
  selected = false,
  onClick,
  disabled = false,
  className = "",
}: TagProps) {
  const cls = COLOR_CLS[color];

  if (!onClick) {
    return (
      <span
        className={`inline-flex items-center px-2 py-0.5 rounded-badge text-xs ${cls.base} ${className}`}
      >
        {children}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={selected}
      className={`inline-flex items-center min-h-[44px] px-4 rounded-badge text-sm
                  border transition-colors disabled:opacity-50
                  ${selected ? `${cls.active} border-transparent` : `${cls.base} border-border`}
                  ${className}`}
    >
      {children}
    </button>
  );
}
