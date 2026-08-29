// lib/weekend.ts
// 周末标签共享 helper（DD-13 §7.2）：从 time_windows 派生「8/8–8/9」式前缀，取不到回退「当周」。
// 供 PlanCard（活动列表标题）与 CityCard（活动次数字段名）使用——不硬编码「本周末」：
// 用户规划的可能是下周末或指定日期，文案必须与 time_windows 一致，取不到就老实说「当周」。

export interface TimeWindowsLike {
  depart?: string;
  return?: string;
}

/** 活动列表标题的周末前缀：从 time_windows 取目标周末（"8/8–8/9 "），取不到回退"当周"。 */
export function weekendLabel(tw?: TimeWindowsLike | null): string {
  const fmt = (iso?: string) => {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : `${d.getMonth() + 1}/${d.getDate()}`;
  };
  const s = fmt(tw?.depart);
  const e = fmt(tw?.return);
  if (s && e) return `${s}–${e} `;
  if (s) return `${s} 起周末 `;
  return "当周";
}
