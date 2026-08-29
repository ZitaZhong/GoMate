// components/cards/PlanCard.tsx
// 计划卡（DD-13 §7.2 + §3.3 Trip Bundle）：探索版概览 + 确认版时间线/预算分区/风险。
// 所有 {value, evidence} 字段一律 FactField（DD-03 红线）；
// 预算分区硬要求：已确认花费（confirmed_by_user）绿区 / 预估待花（estimated）黄区，视觉可辨。
import type {
  BundleActivity,
  CostBlock,
  Evidence,
  ExploreBlock,
  FieldData,
  RawExploreActivity,
  TripBundle,
} from "@/lib/types";
import { FactField } from "@/components/evidence/FactField";
import { Tag } from "@/components/ui/Tag";
import { weekendLabel } from "@/lib/weekend";

/** 探索版活动为纯文本字段 + 活动级 evidence，包装成 FieldData 走 FactField（DD-03 红线不变）。 */
function toField(value: unknown, evidence?: Evidence): FieldData {
  return { value, evidence };
}

const KIND_LABELS: Record<string, string> = {
  transport: "交通",
  activity: "活动",
  meal: "餐饮",
  dining: "餐饮",
  lodging: "住宿",
  buffer: "缓冲",
};

function centsText(cents?: number | null): string {
  if (cents == null) return "—";
  return `¥${(cents / 100).toFixed(2)}`;
}

function fmtDateTime(v: unknown): string {
  if (typeof v !== "string") return String(v ?? "");
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function budgetBandText(v: unknown): string {
  if (v && typeof v === "object") {
    const band = v as { min?: number; max?: number; currency?: string };
    if (band.min != null || band.max != null) {
      return `${band.currency === "CNY" || !band.currency ? "¥" : ""}${band.min ?? "?"} – ${band.max ?? "?"}`;
    }
  }
  return String(v ?? "");
}

function ActivityItem({ activity }: { activity: BundleActivity }) {
  return (
    <li className="border border-border rounded-card p-2 space-y-1">
      {activity.title && (
        <p className="text-sm font-medium text-primary">
          <FactField field={activity.title} />
        </p>
      )}
      <div className="text-xs text-secondary space-y-0.5">
        {activity.venue && (
          <p>
            地点：<FactField field={activity.venue} />
          </p>
        )}
        {activity.start_at && (
          <p>
            时间：<FactField field={activity.start_at} render={fmtDateTime} />
          </p>
        )}
        {activity.price_text && (
          <p>
            费用：<FactField field={activity.price_text} />
          </p>
        )}
      </div>
      <div className="flex gap-3 pt-0.5">
        {activity.booking_url?.value ? (  // 真值判断：空串 '' 同样不渲染（href='' 会跳回当前页）
          <FactField
            field={activity.booking_url}
            render={(v) => (
              <a
                href={String(v)}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center min-h-[44px] text-xs text-accent-blue hover:underline"
              >
                官方预约入口 →
              </a>
            )}
          />
        ) : null}
        {activity.map?.amap_url && (
          <a
            href={activity.map.amap_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center min-h-[44px] text-xs text-accent-blue hover:underline"
          >
            高德地图 →
          </a>
        )}
      </div>
    </li>
  );
}

/** 探索版（后端 compose 实际顶层键）：目的地/主题/时间窗/预算/住宿/活动列表/待确认清单。 */
function RawExploreSection({ bundle }: { bundle: TripBundle }) {
  const cities = (bundle.cities ?? []).map((c) => c?.name).filter(Boolean) as string[];
  const acts = bundle.activities ?? [];
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        {cities.length > 0 && (
          <span className="text-base font-medium text-primary">{cities[0]}</span>
        )}
        {bundle.theme && <Tag color="green">{bundle.theme}</Tag>}
        {cities.slice(1).map((n) => (
          <Tag key={n} color="neutral">{n}</Tag>
        ))}
      </div>
      {bundle.research_outcome && !["initial", "none", "pending"].includes(bundle.research_outcome) && (
        <p className="text-xs text-secondary">{bundle.research_outcome}</p>
      )}

      <dl className="space-y-1 text-sm">
        {bundle.time_windows?.depart && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">出发窗口</dt>
            <dd><FactField field={toField(bundle.time_windows.depart, bundle.time_windows.evidence)} render={fmtDateTime} /></dd>
          </div>
        )}
        {bundle.time_windows?.return && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">返程窗口</dt>
            <dd><FactField field={toField(bundle.time_windows.return, bundle.time_windows.evidence)} render={fmtDateTime} /></dd>
          </div>
        )}
        {bundle.budget_range && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">预算区间</dt>
            <dd>
              <FactField
                field={toField(
                  bundle.budget_range.min != null || bundle.budget_range.max != null
                    ? `¥${bundle.budget_range.min ?? "?"} – ${bundle.budget_range.max ?? "?"}${bundle.budget_range.per_person ? " /人" : ""}`
                    : bundle.budget_range.note ?? "以官方平台为准",
                  bundle.budget_range.evidence,
                )}
              />
            </dd>
          </div>
        )}
        {bundle.lodging_area?.note && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">住宿区域</dt>
            <dd>
              <FactField
                field={toField(
                  bundle.lodging_area.name
                    ? `${bundle.lodging_area.name}（${bundle.lodging_area.note}）`
                    : bundle.lodging_area.note,
                  bundle.lodging_area.evidence,
                )}
              />
            </dd>
          </div>
        )}
      </dl>

      {/* 推荐活动列表（PRD §04：每个城市值得去的活动）；标题带目标周末日期，不写死"本周末" */}
      {acts.length > 0 && (
        <div className="space-y-1">
          <p className="text-sm font-medium text-primary">{weekendLabel(bundle.time_windows)}值得去（{acts.length}）</p>
          <ul className="space-y-2">
            {acts.map((a, i) => (
              <RawActivityItem key={a.id ?? i} activity={a} />
            ))}
          </ul>
        </div>
      )}

      {bundle.pending_checklist && bundle.pending_checklist.length > 0 && (
        <div className="border border-dashed border-secondary/40 rounded-card p-2">
          <p className="text-xs font-medium text-secondary mb-1">待你确认</p>
          <ul className="text-sm space-y-1">
            {bundle.pending_checklist.map((todo, i) => (
              <li key={i} className="flex gap-2">
                <span aria-hidden="true">☐</span>
                <span>{todo}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {bundle.warnings && bundle.warnings.length > 0 && (
        <ul className="text-xs text-secondary space-y-0.5">
          {bundle.warnings.map((w, i) => (
            <li key={i}>⚠ {w}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RawActivityItem({ activity: a }: { activity: RawExploreActivity }) {
  // 展期/长期活动（跨天 end_at，如展览、快闪、连载演出）：显示展期区间而非首日开始时间，
  // 避免"推下周末却显示 7/22"的误解（联调用户反馈）；单场活动仍显示具体场次时间。
  const isRange = (() => {
    if (!a.start_at || !a.end_at) return false;
    const s = new Date(a.start_at), e = new Date(a.end_at);
    return !Number.isNaN(s.getTime()) && !Number.isNaN(e.getTime()) &&
      (e.getTime() - s.getTime()) > 24 * 3600 * 1000;
  })();
  // 展期必须带年份：跨年时"8/29 – 8/30"会误读为不在目标周末内的两天活动（用户实测反馈）。
  // 起点始终 YYYY/M/D；终点同年省年份（"2026/2/3 – 12/31"），跨年补全（"2025/8/29 – 2026/8/30"）。
  const fmtDay = (iso: string, withYear: boolean) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const md = `${d.getMonth() + 1}/${d.getDate()}`;
    return withYear ? `${d.getFullYear()}/${md}` : md;
  };
  const rangeText = (() => {
    if (!isRange) return "";
    const s = new Date(a.start_at!), e = new Date(a.end_at!);
    const crossYear = s.getFullYear() !== e.getFullYear();
    return `${fmtDay(a.start_at!, true)} – ${fmtDay(a.end_at!, crossYear)}`;
  })();
  return (
    <li className="border border-border rounded-card p-2 space-y-1">
      {a.title && (
        <p className="text-sm font-medium text-primary">
          <FactField field={toField(a.title, a.evidence)} />
        </p>
      )}
      <div className="text-xs text-secondary space-y-0.5">
        {a.venue && <p>地点：<FactField field={toField(a.venue, a.evidence)} /></p>}
        {isRange ? (
          <p>展期：<FactField field={toField(rangeText, a.evidence)} /></p>
        ) : (
          a.start_at && (
            <p>时间：<FactField field={toField(a.start_at, a.evidence)} render={fmtDateTime} /></p>
          )
        )}
        {a.price_text && <p>费用：<FactField field={toField(a.price_text, a.evidence)} /></p>}
        {a.category && <p>类型：{a.category}</p>}
      </div>
      {(a.booking_url || a.evidence?.source_url) && (
        <div className="pt-0.5">
          <a
            // 注意用 ||：后端 booking_url 可能是空串 ''，?? 不兜底会生成 href='' 跳回当前页（联调实测 bug）
            href={a.booking_url || a.evidence?.source_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center min-h-[44px] text-xs text-accent-blue hover:underline"
          >
            官方页面 →
          </a>
        </div>
      )}
    </li>
  );
}

function ExploreSection({ explore }: { explore: ExploreBlock }) {
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        {explore.destination && (
          <span className="text-base font-medium text-primary">
            <FactField field={explore.destination} />
          </span>
        )}
        {explore.theme && <Tag color="green">{explore.theme}</Tag>}
      </div>

      <dl className="space-y-1 text-sm">
        {explore.recommended_transport?.mode && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">推荐交通</dt>
            <dd>
              <FactField field={explore.recommended_transport.mode} />
              {explore.recommended_transport.reason && (
                <span className="block text-xs text-secondary mt-0.5">
                  {explore.recommended_transport.reason}
                </span>
              )}
            </dd>
          </div>
        )}
        {explore.depart_window && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">出发窗口</dt>
            <dd>
              <FactField field={explore.depart_window} />
            </dd>
          </div>
        )}
        {explore.return_window && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">返程窗口</dt>
            <dd>
              <FactField field={explore.return_window} />
            </dd>
          </div>
        )}
        {explore.budget_band && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">预算区间</dt>
            <dd>
              <FactField field={explore.budget_band} render={budgetBandText} />
            </dd>
          </div>
        )}
        {explore.lodging_area && (
          <div className="flex gap-2">
            <dt className="text-secondary shrink-0">住宿区域</dt>
            <dd>
              <FactField field={explore.lodging_area} />
            </dd>
          </div>
        )}
      </dl>

      {explore.core_activities && explore.core_activities.length > 0 && (
        <ul className="space-y-2">
          {explore.core_activities.map((a, i) => (
            <ActivityItem key={i} activity={a} />
          ))}
        </ul>
      )}

      {explore.todo_checklist && explore.todo_checklist.length > 0 && (
        <div className="border border-dashed border-secondary/40 rounded-card p-2">
          <p className="text-xs font-medium text-secondary mb-1">待你确认</p>
          <ul className="text-sm space-y-1">
            {explore.todo_checklist.map((todo, i) => (
              <li key={i} className="flex gap-2">
                <span aria-hidden="true">{todo.done ? "☑" : "☐"}</span>
                <span className={todo.done ? "line-through text-secondary" : ""}>
                  {todo.text}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** 预算分区（DD-13 硬要求）：confirmed 绿区 / estimated 黄区，一眼可辨。 */
function CostZone({ title, cost, variant }: {
  title: string;
  cost: CostBlock;
  variant: "confirmed" | "estimated";
}) {
  const zoneCls =
    variant === "confirmed"
      ? "border-accent-green/50 bg-accent-green/10"
      : "border-accent-yellow/60 bg-accent-yellow/15";
  return (
    <div className={`border rounded-card p-2 space-y-1 ${zoneCls}`}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-primary">{title}</p>
        <Tag color={variant === "confirmed" ? "green" : "yellow"}>
          {variant === "confirmed" ? "你已确认" : "估算"}
        </Tag>
      </div>
      <ul className="text-sm space-y-0.5">
        {(cost.items ?? []).map((item, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span>{item.label}</span>
            <FactField
              field={{ value: centsText(item.amount_cents), evidence: item.evidence }}
            />
          </li>
        ))}
      </ul>
      <p className="text-sm font-medium text-primary text-right">
        小计 {centsText(cost.total_cents)}
      </p>
    </div>
  );
}

export interface PlanCardProps {
  bundle: TripBundle;
}

export function PlanCard({ bundle }: PlanCardProps) {
  const isConfirm = bundle.version === "confirm";
  const confirm = bundle.confirm;

  return (
    <div className="bg-card border border-border rounded-card p-3 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        {bundle.title && <h3 className="font-medium text-primary">{bundle.title}</h3>}
        <Tag color={isConfirm ? "green" : "yellow"}>{isConfirm ? "确认版" : "探索版"}</Tag>
      </div>
      {bundle.summary && <p className="text-sm text-secondary">{bundle.summary}</p>}

      {bundle.explore && <ExploreSection explore={bundle.explore} />}
      {/* 后端 compose 的探索版：顶层键（activities/time_windows/budget_range 等） */}
      {!bundle.explore && bundle.version === "explore" && (
        <RawExploreSection bundle={bundle} />
      )}

      {confirm?.timeline && confirm.timeline.length > 0 && (
        <section>
          <p className="text-sm font-medium text-primary mb-1">时间线</p>
          <ol className="space-y-1">
            {confirm.timeline.map((slot, i) => (
              <li key={slot.seq ?? i} className="flex items-center gap-2 text-sm flex-wrap">
                {slot.kind && (
                  <Tag color="neutral">{KIND_LABELS[slot.kind] ?? slot.kind}</Tag>
                )}
                <span className="text-primary">{slot.title}</span>
                {(slot.start_at || slot.end_at) && (
                  <span className="text-xs text-secondary">
                    {slot.start_at && (
                      <FactField field={slot.start_at} render={fmtDateTime} />
                    )}
                    {slot.end_at && (
                      <>
                        {" – "}
                        <FactField field={slot.end_at} render={fmtDateTime} />
                      </>
                    )}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {(confirm?.confirmed_cost || confirm?.estimated_cost) && (
        <section className="space-y-2">
          <p className="text-sm font-medium text-primary">预算</p>
          {confirm?.confirmed_cost && (
            <CostZone title="已确认花费" cost={confirm.confirmed_cost} variant="confirmed" />
          )}
          {confirm?.estimated_cost && (
            <CostZone title="预估待花" cost={confirm.estimated_cost} variant="estimated" />
          )}
        </section>
      )}

      {confirm?.risks && confirm.risks.length > 0 && (
        <section>
          <p className="text-sm font-medium text-primary mb-1">风险提示</p>
          <ul className="text-sm space-y-1">
            {confirm.risks.map((risk, i) => (
              <li key={i} className="flex gap-2 items-start">
                <Tag color={risk.level === "warn" ? "yellow" : "red"} className="shrink-0 mt-0.5">
                  {risk.level === "warn" ? "注意" : "风险"}
                </Tag>
                <span>{risk.text}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {confirm?.alternatives && confirm.alternatives.length > 0 && (
        <section>
          <p className="text-sm font-medium text-primary mb-1">备选方案</p>
          <ul className="text-sm space-y-1 list-disc list-inside">
            {confirm.alternatives.map((alt, i) => (
              <li key={i}>
                {alt.for && <span className="text-secondary">{alt.for}：</span>}
                {alt.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {bundle.reminders_preview && bundle.reminders_preview.length > 0 && (
        <section>
          <p className="text-sm font-medium text-primary mb-1">将提醒你</p>
          <ul className="text-sm space-y-1 list-disc list-inside">
            {bundle.reminders_preview.map((r, i) => (
              <li key={i}>{r.title ?? r.body ?? "提醒"}</li>
            ))}
          </ul>
        </section>
      )}

      {bundle.disclaimer && (
        <p className="text-xs text-secondary border-t border-border pt-2">{bundle.disclaimer}</p>
      )}
    </div>
  );
}
