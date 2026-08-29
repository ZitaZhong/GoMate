// components/itinerary/VerticalTimeline.tsx
// 垂直时间轴（DD-19 §3.6）：竖线 + 圆点 + ItineraryNode 列表，
// 节点操作（替换/删除/调整时间）经 onNodeAction 上抛给 ModifyInput 预填。
"use client";

import { motion } from "framer-motion";
import type { TimelineSlot } from "@/lib/types";
import { ItineraryNode, type NodeAction } from "./ItineraryNode";

export type { NodeAction };

export interface VerticalTimelineProps {
  slots: TimelineSlot[];
  /** (slot, action) => 预填 ModifyInput */
  onNodeAction?: (slot: TimelineSlot, action: NodeAction) => void;
}

export function VerticalTimeline({ slots, onNodeAction }: VerticalTimelineProps) {
  return (
    <div className="relative pl-6 space-y-4">
      {/* 竖线 */}
      <div className="absolute left-2.5 top-2 bottom-2 w-px bg-border" aria-hidden="true" />

      {slots.map((slot, i) => (
        <motion.div
          key={slot.seq}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.05, duration: 0.25 }}
          className="relative"
        >
          {/* 圆点 */}
          <div
            className="absolute -left-3.5 top-1 w-3 h-3 rounded-full bg-accent-green border-2 border-card"
            aria-hidden="true"
          />
          <ItineraryNode slot={slot} onAction={onNodeAction} />
        </motion.div>
      ))}
    </div>
  );
}
