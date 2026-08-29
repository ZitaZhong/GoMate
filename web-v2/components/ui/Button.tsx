// components/ui/Button.tsx
// 基础按钮（设计令牌：bg-card/border-border/rounded-card/text-primary）。
// 移动端硬约束：所有交互区域 >= 44px。
import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "accent" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANT_CLS: Record<Variant, string> = {
  primary: "bg-primary text-white hover:bg-primary/90",
  accent: "bg-accent-green text-white hover:bg-accent-green/90",
  secondary: "bg-card border border-border text-primary hover:bg-background",
  ghost: "text-secondary hover:bg-black/5",
  danger: "bg-accent-red text-white hover:bg-accent-red/90",
};

const SIZE_CLS: Record<Size, string> = {
  sm: "min-h-[44px] px-3 text-xs",
  md: "min-h-[44px] px-4 py-2 text-sm",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  fullWidth?: boolean;
}

export function Button({
  variant = "primary",
  size = "md",
  fullWidth = false,
  className = "",
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-1 rounded-card font-medium
                  transition-colors disabled:opacity-50 disabled:pointer-events-none
                  ${VARIANT_CLS[variant]} ${SIZE_CLS[size]}
                  ${fullWidth ? "w-full" : ""} ${className}`}
      {...rest}
    />
  );
}
