// components/itinerary/ItineraryNode.tsx
// 时间轴单个节点卡片：Lucide 图标 + 标题 + 时间（FactField）+ 备注 +
// PRD §12.5 节点操作（查看地图[高德 URI 深链] / 替换 / 删除 / 调整时间）。
import { Circle, Coffee, MapPin, Palette, Train, UtensilsCrossed } from "lucide-react";
import { FactField } from "@/components/evidence/FactField";
import type { TimelineSlot } from "@/lib/types";

export type NodeAction = "替换" | "删除" | "调整时间";

const KIND_ICON = {
  transport: Train,
  activity: Palette,
  meal: UtensilsCrossed,
  gathering: MapPin,
  free: Coffee,
} as const;

export interface ItineraryNodeProps {
  slot: TimelineSlot;
  /** 提供则渲染 替换/删除/调整时间 操作（预填 ModifyInput） */
  onAction?: (slot: TimelineSlot, action: NodeAction) => void;
}

export function ItineraryNode({ slot, onAction }: ItineraryNodeProps) {
  const Icon = KIND_ICON[slot.kind] ?? Circle;

  return (
    <div className="bg-card border border-border rounded-card p-3">
      <div className="flex items-center gap-2">
        <Icon className="w-4 h-4 text-secondary shrink-0" />
        <span className="font-medium text-sm text-primary">{slot.title}</span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1 text-xs text-secondary">
        <FactField
          field={slot.start_at}
          render={(v) => formatShortTime(v as string)}
        />
        {slot.end_at && (
          <FactField
            field={slot.end_at}
            render={(v) => `~ ${formatShortTime(v as string)}`}
          />
        )}
        {slot.duration_min != null && (
          <span>
            {/* 异常时长兜底：>12h 多为展期/全天活动，显示语义化文案而非分钟数（修 42450 分钟） */}
            {slot.duration_min > 720
              ? "全天/展期"
              : slot.duration_min >= 120
                ? `约 ${Math.round(slot.duration_min / 60)} 小时`
                : `${slot.duration_min} 分钟`}
          </span>
        )}
        {slot.transport_mode && <span>{slot.transport_mode}</span>}
      </div>
      {slot.note && <p className="text-xs text-secondary mt-1">{slot.note}</p>}

      {/* 节点操作（PRD §12.5） */}
      <div className="flex flex-wrap gap-2 mt-2 text-xs">
        {slot.poi_name && (
          <a
            href={`https://uri.amap.com/search?keyword=${encodeURIComponent(slot.poi_name)}`}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center min-h-[32px] px-2 py-1 rounded border border-border
                       text-secondary hover:bg-background"
          >
            查看地图
          </a>
        )}
        {onAction &&
          (["替换", "删除", "调整时间"] as const).map((a) => (
            <button
              key={a}
              onClick={() => onAction(slot, a)}
              className="min-h-[32px] px-2 py-1 rounded border border-border
                         text-secondary hover:bg-background"
            >
              {a}
            </button>
          ))}
      </div>
    </div>
  );
}

function formatShortTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
