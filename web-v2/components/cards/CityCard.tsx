// components/cards/CityCard.tsx
// 城市卡（DD-13 §7.2）：目的地名/推荐理由/量化字段静态渲染。
// 所有 {value, evidence} 字段一律 FactField（DD-03 红线）；
// name/reason 为纯文案，name 用卡片级 evidence 包成 FieldData 走 FactField。
// 地图=高德深链（DD-13 §7.4，不嵌交互地图）。
import type { CandidateCity, FieldData } from "@/lib/types";
import { FactField } from "@/components/evidence/FactField";
import { weekendLabel, type TimeWindowsLike } from "@/lib/weekend";

export interface CityCardProps {
  city: CandidateCity;
  /** 目标周末时间窗（discover 负载携带时传入）；缺省 → 标签回退「当周活动」，不写死「本周末」 */
  timeWindows?: TimeWindowsLike;
}

function minutesText(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n <= 0) return String(v ?? "");
  const h = Math.floor(n / 60);
  const m = Math.round(n % 60);
  return h > 0 ? `约 ${h} 小时${m > 0 ? ` ${m} 分` : ""}` : `约 ${m} 分钟`;
}

export function CityCard({ city, timeWindows }: CityCardProps) {
  const nameField: FieldData = { value: city.name ?? "未知城市", evidence: city.evidence };
  const amapUrl =
    city.map?.amap_url ??
    (city.center
      ? `https://uri.amap.com/marker?position=${city.center[0]},${city.center[1]}&name=${encodeURIComponent(city.name ?? "")}`
      : null);

  return (
    <div className="bg-card border border-border rounded-card p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-medium text-primary text-base">
          <FactField field={nameField} />
        </h3>
        {amapUrl && (
          <a
            href={amapUrl}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 inline-flex items-center min-h-[44px] px-2 text-xs text-accent-blue hover:underline"
          >
            高德地图 →
          </a>
        )}
      </div>

      {city.reason && <p className="text-sm text-secondary">{city.reason}</p>}

      <dl className="space-y-1 text-sm">
        {city.driven_by_activities && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">{weekendLabel(timeWindows)}活动</dt>
            <dd>
              <FactField
                field={city.driven_by_activities}
                render={(v) => `${String(v)} 场`}
              />
            </dd>
          </div>
        )}
        {city.recommended_transport && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">门到门</dt>
            <dd>
              <FactField field={city.recommended_transport} />
            </dd>
          </div>
        )}
        {city.effective_play && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">有效游玩</dt>
            <dd>
              <FactField field={city.effective_play} render={minutesText} />
            </dd>
          </div>
        )}
        {city.budget_estimate && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">预算参考</dt>
            <dd>
              <FactField field={city.budget_estimate} />
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}
