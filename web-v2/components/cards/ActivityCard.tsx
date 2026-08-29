// components/cards/ActivityCard.tsx
// 活动推荐卡片（DD-19 §3.3，字段要求 PRD §7.4.7）：
// 封面懒加载 / 预约状态徽标 / 来源+更新时间可见行 / 通勤对比条 / 选这个+换一个+详情。
// 事实字段一律经 FactField 渲染（AGENTS.md 硬性约束 1）。
// 后端 _cand_dict 目前不产出 cover_url/indoor/booking_status/reason/source_name，
// 这些 props 均为可选——有则渲染，无不渲染（以后端代码为准，不虚构）。
"use client";

import { FactField, type FieldData } from "@/components/evidence/FactField";
import { CommuteBar } from "./CommuteBar";

export type BookingStatus = "可预约" | "无需预约" | "已约满" | "状态未知";

export interface ActivityCardData {
  id: string;
  title: FieldData;
  venue: FieldData;
  start_at: FieldData;
  price_text: FieldData;
  /** 活动封面（懒加载，无图时占位色块） */
  cover_url?: string;
  category?: string | null;
  indoor?: boolean;
  /** PRD §15.1/§7.4.7 预约状态徽标 */
  booking_status?: BookingStatus;
  booking_url?: FieldData;
  /** AI 推荐理由（后端保证具体化；缺省时由调用方用真实字段合成或省略） */
  reason?: string;
  /** 各成员通勤时间 */
  commute_times?: { nickname: string; minutes: number }[];
  match_score?: number;
  /** 信息来源名称（可见展示，非 tooltip） */
  source_name?: string;
  /** 信息更新时间（可见展示） */
  fetched_at?: string;
}

export interface ActivityCardProps {
  activity: ActivityCardData;
  onSelect?: () => void;
  onSwap?: () => void;
  /** 查看详情 → /room/[id]/activity/[aid] */
  onDetail?: () => void;
}

export function ActivityCard({ activity, onSelect, onSwap, onDetail }: ActivityCardProps) {
  const commutes = activity.commute_times ?? [];
  const maxCommute = commutes.reduce((m, c) => Math.max(m, c.minutes), 0);

  return (
    <div className="bg-card border border-border rounded-card overflow-hidden">
      {/* 封面（懒加载；无图时占位色块） */}
      {activity.cover_url ? (
        // eslint-disable-next-line @next/next/no-img-element -- 外链活动封面，懒加载即可
        <img
          src={activity.cover_url}
          alt=""
          loading="lazy"
          className="w-full h-32 object-cover"
        />
      ) : (
        <div className="w-full h-20 bg-accent-green/15" aria-hidden="true" />
      )}

      <div className="p-4 space-y-3">
        {/* 标题行 */}
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="font-semibold text-primary">
              <FactField field={activity.title} />
            </h3>
            <p className="text-sm text-secondary mt-0.5">
              <FactField field={activity.venue} />
            </p>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            {activity.indoor !== undefined && (
              <span className="text-xs px-2 py-0.5 rounded-badge bg-accent-green/15 text-primary">
                {activity.indoor ? "室内" : "户外"}
              </span>
            )}
            {activity.booking_status && (
              <span
                className={`text-xs px-2 py-0.5 rounded-badge ${
                  activity.booking_status === "已约满"
                    ? "bg-accent-red/15 text-primary"
                    : "bg-accent-blue/15 text-primary"
                }`}
              >
                {activity.booking_status}
              </span>
            )}
          </div>
        </div>

        {/* 信息行 */}
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-secondary">
          <span>
            <FactField
              field={activity.start_at}
              render={(v) => formatTime(v as string)}
            />
          </span>
          <span>
            <FactField field={activity.price_text} />
          </span>
          {activity.category && <span>{activity.category}</span>}
          {activity.match_score !== undefined && (
            <span>匹配度 {Math.round(activity.match_score * 100)}%</span>
          )}
        </div>

        {/* AI 推荐理由（PRD §7.4.7） */}
        {activity.reason && (
          <p className="text-sm text-primary/80 bg-accent-yellow/15 px-3 py-2 rounded-lg">
            {activity.reason}
          </p>
        )}

        {/* 通勤对比条（PRD §7.4.6 公平性表达） */}
        {commutes.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-secondary">各成员通勤时间：</p>
            {commutes.map((c) => (
              <CommuteBar
                key={c.nickname}
                nickname={c.nickname}
                minutes={c.minutes}
                max={maxCommute}
              />
            ))}
            <p className="text-xs text-primary">
              最远成员需 <span className="font-medium">{maxCommute}</span> 分钟
            </p>
          </div>
        )}

        {/* 来源与更新时间（可见展示，PRD §7.4.7） */}
        {(activity.source_name || activity.fetched_at) && (
          <p className="text-xs text-secondary">
            来源：{activity.source_name ?? "官方页面"}
            {activity.fetched_at && ` · 更新于 ${activity.fetched_at}`}
          </p>
        )}

        {/* 操作按钮（移动端 >= 44px） */}
        <div className="flex gap-2 pt-1">
          {onSelect && (
            <button
              onClick={onSelect}
              className="flex-1 min-h-[44px] py-2 bg-accent-green text-white rounded-lg text-sm font-medium
                         hover:bg-accent-green/90 transition"
            >
              选这个
            </button>
          )}
          {onSwap && (
            <button
              onClick={onSwap}
              className="min-h-[44px] px-4 py-2 border border-border rounded-lg text-sm text-secondary
                         hover:bg-background transition"
            >
              换一个
            </button>
          )}
          {onDetail && (
            <button
              onClick={onDetail}
              className="min-h-[44px] px-4 py-2 border border-border rounded-lg text-sm text-secondary
                         hover:bg-background transition"
            >
              详情
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso.slice(0, 16);
  }
}
