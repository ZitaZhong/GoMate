// components/itinerary/slotMapping.ts
// Itinerary（GET /rooms/{id}/plan 负载）→ TimelineSlot[]（VerticalTimeline 视图模型）。
// kind 映射：gathering→gathering / activity→activity / dining→meal / transport→transport / 其他→free。
// 时间字段带 evidence 时包成 FieldData 走 FactField；无 evidence 按 unknown 兜底渲染。
import type { Itinerary, ItineraryNode, TimelineSlot } from "@/lib/types";

const KIND_MAP: Record<string, TimelineSlot["kind"]> = {
  gathering: "gathering",
  activity: "activity",
  dining: "meal",
  transport: "transport",
};

function nodeKind(type: string): TimelineSlot["kind"] {
  return KIND_MAP[type] ?? "free";
}

function durationMin(node: ItineraryNode): number | undefined {
  if (!node.start || !node.end) return undefined;
  const ms = new Date(node.end).getTime() - new Date(node.start).getTime();
  if (!Number.isFinite(ms) || ms <= 0) return undefined;
  return Math.round(ms / 60000);
}

export function itineraryToSlots(itinerary: Itinerary): TimelineSlot[] {
  return (itinerary.nodes ?? []).map((n, i) => ({
    seq: i + 1,
    kind: nodeKind(n.type),
    title: n.title,
    start_at: { value: n.start ?? null, evidence: n.evidence },
    end_at:
      n.end != null ? { value: n.end, evidence: n.evidence } : undefined,
    duration_min: durationMin(n),
    note: n.note,
    // 高德 URI 深链用地点名（优先 venue，回退标题）
    poi_name: n.venue ?? n.title,
  }));
}
