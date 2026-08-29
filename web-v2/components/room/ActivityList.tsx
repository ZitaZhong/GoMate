// components/room/ActivityList.tsx
// 活动候选列表：ActivityCard 逐个出现动效（200-300ms，DD-19 §2.4）。
"use client";

import { motion } from "framer-motion";
import { ActivityCard, type ActivityCardData } from "@/components/cards/ActivityCard";

export interface ActivityListProps {
  activities: ActivityCardData[];
  selectingId?: string | null;
  onSelect?: (a: ActivityCardData) => void;
  onSwap?: (a: ActivityCardData) => void;
  onDetail?: (a: ActivityCardData) => void;
}

export function ActivityList({
  activities,
  selectingId,
  onSelect,
  onSwap,
  onDetail,
}: ActivityListProps) {
  if (activities.length === 0) return null;
  return (
    <div className="space-y-3">
      {activities.map((a, i) => (
        <motion.div
          key={a.id}
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.06, duration: 0.25 }}
          className={selectingId === a.id ? "opacity-60 pointer-events-none" : ""}
        >
          <ActivityCard
            activity={a}
            onSelect={onSelect ? () => onSelect(a) : undefined}
            onSwap={onSwap ? () => onSwap(a) : undefined}
            onDetail={onDetail ? () => onDetail(a) : undefined}
          />
        </motion.div>
      ))}
    </div>
  );
}
