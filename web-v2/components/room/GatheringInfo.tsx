// components/room/GatheringInfo.tsx
// 集合信息（DD-18 plan_gathering 产出）：集合点 + 目标时间 + 各成员倒推出发时间。
// 出发/到达时间为算法估算，整体标注「估算」徽标（证据语义，非事实字段）。
import { EvidenceBadge } from "@/components/evidence/EvidenceBadge";
import type { Gathering } from "@/lib/types";

export interface GatheringInfoProps {
  gathering: Gathering | null;
}

const hhmm = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
};

const POINT_TYPE_LABEL: Record<string, string> = {
  entrance: "场馆入口",
  metro: "地铁站",
  venue: "活动地点",
};

export function GatheringInfo({ gathering }: GatheringInfoProps) {
  if (!gathering || (!gathering.gathering_point && !gathering.target_time)) {
    return null;
  }
  const point = gathering.gathering_point;

  return (
    <section className="bg-card border border-border rounded-card p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-primary">集合信息</h3>
        <EvidenceBadge status="estimated" />
      </div>
      <p className="text-sm text-primary">
        {point ? (
          <>
            {point.name}
            <span className="text-xs text-secondary ml-1">
              ({POINT_TYPE_LABEL[point.type] ?? "集合点"})
            </span>
          </>
        ) : (
          "集合点待定"
        )}
        {gathering.target_time && (
          <span className="text-secondary"> · {hhmm(gathering.target_time)} 集合</span>
        )}
      </p>
      {point?.name && (
        <a
          href={`https://uri.amap.com/search?keyword=${encodeURIComponent(point.name)}`}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center min-h-[44px] px-3 rounded-lg border border-border
                     text-sm text-secondary hover:bg-background"
        >
          在高德地图中查看
        </a>
      )}
      {gathering.member_departures.length > 0 && (
        <ul className="space-y-1.5">
          {gathering.member_departures.map((d) => (
            <li
              key={d.member_id}
              className="flex items-center justify-between gap-2 text-sm"
            >
              <span className="text-primary">{d.nickname}</span>
              <span className="text-secondary text-xs">
                {hhmm(d.suggested_departure)} 出发 · {d.transport_mode} 约{" "}
                {d.duration_min} 分钟
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
