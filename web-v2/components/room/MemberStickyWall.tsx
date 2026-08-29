// components/room/MemberStickyWall.tsx
// 便签墙（DD-19 §3.5）：成员便签卡（300-400ms 落入动效）+ AI 总结块。
// 成员便签用 RoomMember（含 hard_constraints，来自 GET /rooms/{id}，出口已脱敏）。
"use client";

import { motion } from "framer-motion";
import type { RoomMember } from "@/lib/types";

export interface StickySummary {
  /** 共同空闲文本，如 "14:00 ~ 21:00（约 7 小时）" */
  common_window: string;
  shared_interests: string[];
  conflicts: string[];
}

export interface MemberStickyWallProps {
  members: RoomMember[];
  summary?: StickySummary | null;
}

const STICKY_COLORS = [
  "bg-accent-yellow/20",
  "bg-accent-blue/20",
  "bg-accent-coral/20",
  "bg-accent-green/20",
];

export function MemberStickyWall({ members, summary }: MemberStickyWallProps) {
  return (
    <div className="space-y-4">
      {/* 便签卡 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {members.map((m, i) => (
          <motion.div
            key={m.member_id}
            initial={{ opacity: 0, y: 20, rotate: -2 }}
            animate={{ opacity: 1, y: 0, rotate: i % 2 === 0 ? -1 : 1 }}
            transition={{ delay: i * 0.1, duration: 0.35 }}
            className={`p-4 rounded-card border border-border shadow-sm ${
              STICKY_COLORS[i % STICKY_COLORS.length]
            } ${m.submitted ? "" : "opacity-70"}`}
          >
            <div className="flex items-center justify-between gap-2">
              <p className="font-semibold text-primary">{m.nickname}</p>
              {!m.submitted && (
                <span className="text-xs px-2 py-0.5 rounded-badge border border-dashed border-secondary/40 text-secondary">
                  待填写
                </span>
              )}
            </div>
            <p className="text-sm text-secondary mt-1">
              {m.origin_name ? `${m.origin_name} 出发` : "出发地未填"}
            </p>
            <p className="text-sm text-secondary">
              {m.earliest_depart && m.latest_end
                ? `${m.earliest_depart} ~ ${m.latest_end}`
                : "时间未填"}
            </p>
            {m.interests.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {m.interests.map((tag) => (
                  <span
                    key={tag}
                    className="text-xs px-2 py-0.5 rounded-badge bg-card border border-border"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            )}
            {m.hard_constraints.length > 0 && (
              <p className="text-xs text-accent-red mt-1">
                {m.hard_constraints.join("、")}
              </p>
            )}
          </motion.div>
        ))}
      </div>

      {/* AI 总结 */}
      {summary && (
        <div className="bg-card border border-border rounded-card p-4">
          <p className="text-sm font-medium text-primary">AI 总结</p>
          <p className="text-sm text-secondary mt-1">
            共同空闲：{summary.common_window}
          </p>
          {summary.shared_interests.length > 0 && (
            <p className="text-sm text-secondary">
              共同兴趣：{summary.shared_interests.join("、")}
            </p>
          )}
          {summary.conflicts.length > 0 && (
            <div className="mt-2 space-y-1">
              {summary.conflicts.map((c) => (
                <p key={c} className="text-xs text-accent-coral">
                  {c}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
