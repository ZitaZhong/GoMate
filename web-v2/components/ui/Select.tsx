// components/ui/Select.tsx
// 基础下拉选择（原生 select，移动端优先：高度 >= 44px）。
import { forwardRef, type SelectHTMLAttributes } from "react";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  error?: string;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { label, error, className = "", id, children, ...rest },
  ref,
) {
  const selectEl = (
    <select
      ref={ref}
      id={id}
      className={`w-full min-h-[44px] px-3 py-2 bg-card border rounded-card text-sm text-primary
                  focus:outline-none focus:ring-2 focus:ring-accent-green/50
                  disabled:opacity-50
                  ${error ? "border-accent-red" : "border-border"} ${className}`}
      {...rest}
    >
      {children}
    </select>
  );

  if (!label && !error) return selectEl;

  return (
    <label className="block text-sm text-secondary">
      {label}
      <span className={label ? "mt-1 block" : ""}>{selectEl}</span>
      {error && <span className="mt-1 block text-xs text-accent-red">{error}</span>}
    </label>
  );
});
