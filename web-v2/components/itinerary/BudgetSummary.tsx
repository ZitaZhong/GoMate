// components/itinerary/BudgetSummary.tsx
// 行程预算摘要：房间预算区间 + 成员最低预算（硬约束「预算上限」的生效值）。
// 仅用于房间内 plan 页；分享页不得引用（AGENTS.md 脱敏约束：不出个人预算数字）。
import type { CommonWindow } from "@/lib/types";

export interface BudgetSummaryProps {
  /** 房间 budget_range（元由调用方换算） */
  budgetRange?: { min?: number; max?: number; currency?: string } | null;
  /** 成员最低人均预算（分，summary.budget_min；即最严格的预算约束） */
  minMemberBudget?: number | null;
  commonWindow?: CommonWindow | null;
  warnings?: string[];
}

export function BudgetSummary({
  budgetRange,
  minMemberBudget,
  commonWindow,
  warnings,
}: BudgetSummaryProps) {
  const lines: string[] = [];
  if (budgetRange?.max != null) {
    lines.push(`房间预算：人均 ${budgetRange.min ?? 0} ~ ${budgetRange.max} 元`);
  }
  if (minMemberBudget != null) {
    lines.push(`成员最低预算：人均 ${Math.round(minMemberBudget / 100)} 元（按最严格的来）`);
  }
  if (commonWindow?.start && commonWindow?.end) {
    lines.push(
      `共同时间窗：${commonWindow.start} ~ ${commonWindow.end}` +
        (commonWindow.available_hours != null
          ? `（约 ${commonWindow.available_hours} 小时）`
          : ""),
    );
  }
  const warnList = warnings ?? [];
  if (lines.length === 0 && warnList.length === 0) return null;

  return (
    <section className="bg-card border border-border rounded-card p-4 space-y-1.5">
      <h3 className="text-sm font-medium text-primary">预算与时间</h3>
      {lines.map((l) => (
        <p key={l} className="text-sm text-secondary">
          {l}
        </p>
      ))}
      {commonWindow && !commonWindow.feasible && commonWindow.suggestions.length > 0 && (
        <p className="text-xs text-accent-coral">
          时间偏紧：{commonWindow.suggestions.join("；")}
        </p>
      )}
      {warnList.map((w) => (
        <p key={w} className="text-xs text-accent-coral">
          {w}
        </p>
      ))}
    </section>
  );
}
