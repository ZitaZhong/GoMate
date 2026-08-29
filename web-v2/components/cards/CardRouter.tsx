// components/cards/CardRouter.tsx
// CardPayload → 卡片组件路由（DD-19 §4.2 / DD-13 §4.4）。
// 注意：BFF `_sse` 只透传事件的 data 字段（src/wheretogo/bff/app.py），
// 图节点名不进 SSE 帧，因此这里按 data 内容键路由：
//   data.bundle             → done：完整 Trip Bundle 卡（确认版，已过闸三）
//   data.explore_bundle     → interrupt：探索版卡 + 回填入口（BYO Booking）
//   data.transport_options  → node_output transport：TransportCard
//   data.candidate_cities   → node_output discover：CityCard 列表
// 其余节点产物（research/weather/timeline 等中间态）不渲染卡片。
import type {
  CandidateCity,
  CardPayload,
  RoutePlan,
  TransportOptions,
  TripBundle,
} from "@/lib/types";
import type { TimeWindowsLike } from "@/lib/weekend";
import { CityCard } from "./CityCard";
import { TransportCard } from "./TransportCard";
import { PlanCard } from "./PlanCard";
import { RoutePlanCard } from "./RoutePlanCard";

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export interface CardRouterProps {
  payload: CardPayload;
  /** 回填入口链接用；'new' 或缺省时不渲染入口 */
  planId?: string;
}

export function CardRouter({ payload, planId }: CardRouterProps) {
  // 兼容两种形态：{node, data} 包装（DD-19 契约）或 data 平铺（BFF 实际帧）
  const data = (isRecord(payload.data) ? payload.data : payload) as Record<string, unknown>;

  // design_itinerary：锚点路线卡（DD-15 v1.1）
  if (isRecord(data.route_plan)) {
    return <RoutePlanCard plan={data.route_plan as unknown as RoutePlan} />;
  }

  // done：确认版完整 Bundle
  if (isRecord(data.bundle)) {
    return <PlanCard bundle={data.bundle as unknown as TripBundle} />;
  }

  // interrupt：探索版 + 回填入口
  if (isRecord(data.explore_bundle)) {
    const showBackfill = planId && planId !== "new";
    return (
      <div className="space-y-2">
        <PlanCard bundle={data.explore_bundle as unknown as TripBundle} />
        <div className="border border-accent-yellow/60 bg-accent-yellow/15 rounded-card p-3 space-y-1">
          <p className="text-sm text-primary">
            方案已就绪，确认你的车票/酒店后即可生成最终版。
          </p>
          {showBackfill ? (
            <a
              href={`/plan/${encodeURIComponent(planId)}#booking`}
              className="inline-flex items-center min-h-[44px] text-sm text-accent-blue hover:underline"
            >
              去计划页回填订单 →
            </a>
          ) : (
            <p className="text-xs text-secondary">生成计划后可在计划页粘贴订单文本回填。</p>
          )}
        </div>
      </div>
    );
  }

  // node_output transport：交通比较卡
  if (isRecord(data.transport_options)) {
    return <TransportCard options={data.transport_options as unknown as TransportOptions} />;
  }

  // node_output discover：候选城市卡列表
  if (Array.isArray(data.candidate_cities)) {
    const cities = data.candidate_cities.filter(isRecord);
    if (cities.length === 0) return null;
    // 负载带 time_windows 时传给 CityCard 派生周末标签（无则回退「当周活动」）
    const timeWindows = isRecord(data.time_windows)
      ? (data.time_windows as TimeWindowsLike)
      : undefined;
    return (
      <div className="space-y-2">
        {cities.map((city, i) => (
          <CityCard
            key={(city as CandidateCity).city_code ?? i}
            city={city as unknown as CandidateCity}
            timeWindows={timeWindows}
          />
        ))}
      </div>
    );
  }

  return null;
}
