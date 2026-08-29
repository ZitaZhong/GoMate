// components/ui/Input.tsx
// 基础输入框（移动端优先：高度 >= 44px，字号 >= 14px）。
import { forwardRef, type InputHTMLAttributes } from "react";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, error, className = "", id, ...rest },
  ref,
) {
  const inputEl = (
    <input
      ref={ref}
      id={id}
      className={`w-full min-h-[44px] px-3 py-2 bg-card border rounded-card text-sm text-primary
                  placeholder:text-secondary/70
                  focus:outline-none focus:ring-2 focus:ring-accent-green/50
                  disabled:opacity-50
                  ${error ? "border-accent-red" : "border-border"} ${className}`}
      {...rest}
    />
  );

  if (!label && !error) return inputEl;

  return (
    <label className="block text-sm text-secondary">
      {label}
      <span className={label ? "mt-1 block" : ""}>{inputEl}</span>
      {error && <span className="mt-1 block text-xs text-accent-red">{error}</span>}
    </label>
  );
});
