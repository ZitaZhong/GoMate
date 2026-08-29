// components/evidence/EvidenceBadge.tsx
// 证据六态徽标（DD-19 §3.1，supersede DD-13 徽标形态，语义不变）。
// 对比度规则：色相只用于圆点、浅底色（/15 透明度）和边框；
// 徽标文字一律深色（text-primary / text-secondary），保证 ≥ 4.5:1。

interface StatusMeta {
  label: string;
  badgeCls: string;
  dotCls: string;
}

export const EVIDENCE_STATUS_META: Record<string, StatusMeta> = {
  confirmed_by_user: {
    label: "已确认",
    badgeCls: "bg-evidence-confirmed/15 text-primary",
    dotCls: "bg-evidence-confirmed",
  },
  official_source_confirmed: {
    label: "官方确认",
    badgeCls: "bg-evidence-official/15 text-primary",
    dotCls: "bg-evidence-official",
  },
  public_source_observed: {
    label: "公开可查",
    badgeCls: "bg-black/5 text-secondary",
    dotCls: "bg-evidence-observed",
  },
  estimated: {
    label: "估算",
    badgeCls: "bg-evidence-estimated/25 text-primary",
    dotCls: "bg-evidence-estimated",
  },
  unknown: {
    label: "待确认",
    badgeCls: "border border-dashed border-secondary/40 text-secondary",
    dotCls: "bg-evidence-unknown",
  },
  expired: {
    label: "可能过期",
    badgeCls: "bg-evidence-expired/15 text-primary",
    dotCls: "bg-evidence-expired",
  },
};

export function evidenceStatusMeta(status: string | undefined | null): StatusMeta {
  return EVIDENCE_STATUS_META[status ?? "unknown"] ?? EVIDENCE_STATUS_META.unknown;
}

export interface EvidenceBadgeProps {
  /** verification_status；缺省/未知一律按 unknown 渲染（fail-safe） */
  status?: string | null;
  /** 有来源时徽标渲染为可点链接（移动端无 hover，不用 tooltip 承载关键信息） */
  sourceUrl?: string;
  className?: string;
}

export function EvidenceBadge({ status, sourceUrl, className = "" }: EvidenceBadgeProps) {
  const meta = evidenceStatusMeta(status);

  const badge = (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-badge text-xs ${meta.badgeCls} ${className}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${meta.dotCls}`} />
      {meta.label}
    </span>
  );

  if (!sourceUrl) return badge;

  return (
    <a
      href={sourceUrl}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="inline-flex"
    >
      {badge}
    </a>
  );
}
