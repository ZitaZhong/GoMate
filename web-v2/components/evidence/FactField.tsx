// components/evidence/FactField.tsx
// DD-03 证据六态渲染核心组件（硬 KPI：未确认误展为已确认 = 0）。
// 徽标视觉以 DD-19 §3.1 为准（彩色圆点 + 浅底色 badge + 深色文字，对比度 ≥ 4.5:1）；
// 徽标形态复用 components/evidence/EvidenceBadge.tsx。
import type { ReactNode } from "react";
import type { Evidence, FieldData } from "@/lib/types";
import { EvidenceBadge } from "./EvidenceBadge";

export type { Evidence, FieldData };

export interface FactFieldProps {
  field: FieldData;
  render?: (v: unknown) => ReactNode;
  className?: string;
}

export function FactField({ field, render, className = "" }: FactFieldProps) {
  // fail-safe：evidence 缺失或 value 为空一律按 unknown 渲染
  const st = field.evidence?.verification_status ?? "unknown";
  const isUnknown = st === "unknown" || field.value == null;

  return (
    <span className={`inline-flex items-center gap-1.5 flex-wrap ${className}`}>
      {isUnknown ? (
        <em className="text-secondary italic text-sm">请到官方平台确认</em>
      ) : (
        <span
          className={
            st === "estimated" ? "italic" : st === "expired" ? "line-through" : ""
          }
        >
          {render ? render(field.value) : String(field.value)}
        </span>
      )}
      <EvidenceBadge status={st} sourceUrl={field.evidence?.source_url} />
      {st === "expired" && field.evidence?.fetched_at && (
        <small className="text-xs text-secondary">
          更新于 {field.evidence.fetched_at}
        </small>
      )}
    </span>
  );
}
