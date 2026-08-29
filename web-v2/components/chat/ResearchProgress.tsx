// components/chat/ResearchProgress.tsx
// research_progress / progress 事件的进度指示（DD-19 §4.2）。
// 动效 200-500ms（DD-19 §2.4）：容器淡入 300ms，圆点 pulse。
"use client";

import { motion } from "framer-motion";

/** 图节点 phase → 中文进度文案（未知 phase 兜底原文）。 */
const PHASE_LABELS: Record<string, string> = {
  parse: "理解你的需求",
  discover: "挑选目的地城市",
  research: "联网深研活动中",
  reflect: "复盘研究成果",
  transport: "比较门到门交通",
  await_booking: "等待你确认订单",
  hotel: "圈定住宿区域",
  mobility: "规划市内出行",
  dining: "挑选餐饮",
  weather: "查询天气",
  timeline: "编排时间线",
  validate: "校验方案",
  compose: "组装最终方案",
};

export interface ResearchProgressProps {
  phase: string;
  message?: string;
}

export function ResearchProgress({ phase, message }: ResearchProgressProps) {
  const label = PHASE_LABELS[phase] ?? phase;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3 }}
      role="status"
      aria-live="polite"
      className="flex items-center gap-2 px-3 py-2 text-sm text-secondary"
    >
      <span className="flex gap-1" aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <motion.span
            key={i}
            className="w-1.5 h-1.5 rounded-full bg-accent-green"
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.15 }}
          />
        ))}
      </span>
      <span className="font-medium text-primary">{label}</span>
      {message && <span className="truncate">{message}</span>}
    </motion.div>
  );
}
