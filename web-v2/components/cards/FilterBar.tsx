// components/cards/FilterBar.tsx
// 推荐页顶部筛选条件（PRD §12.4）：展示本次推荐的生效条件（城市/日期/主题/
// 共同时间窗/预算上限），并提供本地排序切换。
// 说明：后端 _cand_dict 不产出 indoor 字段，故不提供室内/户外筛选
// （不展示没有数据支撑的筛选条件，以代码为准）。
"use client";

export type RecommendSort = "match" | "commute";

export interface FilterConditions {
  city: string;
  activityDate: string;
  theme?: string | null;
  /** 共同时间窗文本，如 "14:00 ~ 21:00（7h）" */
  commonWindow?: string | null;
  /** 预算上限（元），来自房间 budget_range.max */
  budgetMaxYuan?: number | null;
}

export interface FilterBarProps {
  conditions: FilterConditions;
  sort: RecommendSort;
  onSortChange: (sort: RecommendSort) => void;
}

export function FilterBar({ conditions, sort, onSortChange }: FilterBarProps) {
  const chips: string[] = [
    conditions.city,
    conditions.activityDate,
    conditions.theme ? `主题：${conditions.theme}` : "",
    conditions.commonWindow ? `共同空闲 ${conditions.commonWindow}` : "",
    conditions.budgetMaxYuan != null ? `人均 ≤ ${conditions.budgetMaxYuan} 元` : "",
  ].filter(Boolean);

  return (
    <div className="bg-card border border-border rounded-card p-3 space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <span
            key={c}
            className="text-xs px-2 py-1 rounded-badge bg-accent-blue/15 text-primary"
          >
            {c}
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2 text-xs text-secondary">
        <span>排序：</span>
        {(
          [
            { value: "match", label: "综合推荐" },
            { value: "commute", label: "通勤公平优先" },
          ] as const
        ).map((opt) => (
          <button
            key={opt.value}
            onClick={() => onSortChange(opt.value)}
            aria-pressed={sort === opt.value}
            className={`min-h-[32px] px-3 rounded-badge border transition ${
              sort === opt.value
                ? "bg-primary text-white border-transparent"
                : "border-border hover:bg-background"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  );
}
