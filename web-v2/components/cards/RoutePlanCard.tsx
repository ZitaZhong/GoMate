// components/cards/RoutePlanCard.tsx
// 锚点路线卡（DD-15 v1.1：design_itinerary 产出）
// 证据纪律：库内活动透传 evidence；估算排程/接驳/待确认锚点一律经 FactField 六态渲染。
import { CalendarDays, MapPin, Train, UtensilsCrossed, AlertTriangle } from "lucide-react";
import { FactField, type FieldData } from "@/components/evidence/FactField";
import type { RoutePlan, RouteSlot } from "@/lib/types";

function toField(value: unknown, evidence?: FieldData["evidence"]): FieldData {
  return { value, evidence };
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", {
      timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso.slice(11, 16); }
}

function fmtDay(date: string): string {
  try {
    return new Date(date).toLocaleDateString("zh-CN", {
      timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", weekday: "short",
    });
  } catch { return date; }
}

function SlotRow({ slot }: { slot: RouteSlot }) {
  if (slot.kind === "leg") {
    return (
      <div className="flex items-center gap-2 pl-6 text-xs text-secondary">
        <Train className="w-3.5 h-3.5 shrink-0" />
        <span className="flex-1 min-w-0 truncate">{slot.title}</span>
        {slot.note && <span className="shrink-0">{slot.note}</span>}
      </div>
    );
  }
  const isMeal = slot.kind === "meal";
  return (
    <div className="border border-border rounded-card p-2.5 space-y-1">
      <div className="flex items-start gap-2">
        {isMeal
          ? <UtensilsCrossed className="w-4 h-4 text-secondary shrink-0 mt-0.5" />
          : <MapPin className="w-4 h-4 text-accent-green shrink-0 mt-0.5" />}
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-primary">
            <FactField field={toField(slot.title, slot.evidence)} />
          </p>
          {slot.venue && (
            <p className="text-xs text-secondary mt-0.5">
              <FactField field={toField(slot.venue, slot.evidence)} />
            </p>
          )}
        </div>
        <span className="text-xs text-secondary shrink-0">
          <FactField
            field={toField(`${fmtTime(slot.start)} – ${fmtTime(slot.end)}`, slot.evidence)}
          />
        </span>
      </div>
      {slot.note && <p className="text-xs text-secondary pl-6">{slot.note}</p>}
    </div>
  );
}

export function RoutePlanCard({ plan }: { plan: RoutePlan }) {
  return (
    <div className="bg-card border border-border rounded-card p-3 space-y-3">
      <p className="text-sm font-medium text-primary">
        路线安排（{plan.anchors_resolved} 个锚点
        {plan.anchors_pending.length > 0 && `，${plan.anchors_pending.length} 个待确认`}）
      </p>

      {plan.days.map((day) => (
        <section key={day.date} className="space-y-1.5">
          <p className="flex items-center gap-1.5 text-xs font-medium text-secondary">
            <CalendarDays className="w-3.5 h-3.5" />
            {fmtDay(day.date)}
          </p>
          <div className="space-y-1.5">
            {day.slots.map((slot, i) => (
              <SlotRow key={i} slot={slot} />
            ))}
          </div>
        </section>
      ))}

      {plan.warnings.length > 0 && (
        <ul className="space-y-1 border-t border-border pt-2">
          {plan.warnings.map((w, i) => (
            <li key={i} className="flex gap-1.5 text-xs text-secondary">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-evidence-estimated" />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
