// components/cards/CommuteBar.tsx
// 成员通勤横条（PRD §7.4.6 公平性表达）：比例 = minutes / max，最远成员高亮。
// 纯展示组件；色相只做底色，文字一律深色（DD-19 §3.1 对比度规则）。

export interface CommuteBarProps {
  nickname: string;
  /** 通勤分钟数 */
  minutes: number;
  /** 全组成员中的最大值（用于比例归一；<=0 时按 1 处理） */
  max: number;
}

export function CommuteBar({ nickname, minutes, max }: CommuteBarProps) {
  const safeMax = max > 0 ? max : 1;
  const pct = Math.max(4, Math.round((minutes / safeMax) * 100));
  const isFurthest = minutes === max && max > 0;

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-14 shrink-0 truncate text-secondary">{nickname}</span>
      <div className="flex-1 h-2 rounded-full bg-black/5 overflow-hidden">
        <div
          className={`h-full rounded-full ${
            isFurthest ? "bg-accent-coral" : "bg-accent-blue"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span
        className={`w-12 shrink-0 text-right ${
          isFurthest ? "font-medium text-primary" : "text-secondary"
        }`}
      >
        {minutes} 分钟
      </span>
    </div>
  );
}
