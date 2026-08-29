// components/cards/TransportCard.tsx
// 交通卡（DD-13 §7.2 + DD-09 §3.2-3.6）：门到门比较（高铁/飞机）、推荐方式、
// 铁路/航班查询策略卡 + 官方入口深链（12306/航司，不做交易 UI、不含票价余票）。
// evidence_by_seg 的逐段证据经 FactField；总价/缓冲为估算（DD-09 disclaimer）。
import type {
  DoorToDoor,
  FieldData,
  FlightStrategyCard,
  RailStrategyCard,
  TransportCandidate,
  TransportOptions,
} from "@/lib/types";
import { FactField } from "@/components/evidence/FactField";
import { Tag } from "@/components/ui/Tag";

// 与后端 DD-09 产出对齐（domain/transport.py decide_mode → rail/air/compare；同城 → local）
const MODE_LABELS: Record<string, string> = {
  rail: "高铁优先",
  air: "飞机优先",
  compare: "高铁/飞机都值得比较",
  local: "同城出行",
};

const ESTIMATED_EVIDENCE = { verification_status: "estimated", source_type: "rule" } as const;

function minutesText(min?: number): string {
  if (min == null) return "";
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}

function DoorToDoorBlock({ title, d2d }: { title: string; d2d: DoorToDoor }) {
  return (
    <div className="border border-border rounded-card p-2 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-primary">{title}</p>
        {d2d.total_min != null && (
          <span className="text-sm text-primary">
            门到门 {minutesText(d2d.total_min)}
            <Tag color="yellow" className="ml-1">估算</Tag>
          </span>
        )}
      </div>
      {(d2d.station || d2d.dest_station) && (
        <p className="text-xs text-secondary">
          {d2d.station ?? "?"} → {d2d.dest_station ?? "?"}
        </p>
      )}
      {d2d.legs && d2d.legs.length > 0 && (
        <ul className="text-xs text-secondary space-y-0.5 pt-1">
          {d2d.legs.map((leg, i) => (
            <li key={leg.seg ?? i} className="flex items-center justify-between gap-2">
              <span className="truncate">{leg.label ?? leg.seg}</span>
              {leg.minutes != null && (
                <FactField
                  field={
                    {
                      value: `${leg.minutes} 分`,
                      evidence: d2d.evidence_by_seg?.[leg.seg ?? ""] ?? ESTIMATED_EVIDENCE,
                    } satisfies FieldData
                  }
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RailStrategy({ card }: { card: RailStrategyCard }) {
  return (
    <div className="border border-accent-blue/40 bg-accent-blue/5 rounded-card p-2 space-y-1.5">
      <p className="text-sm font-medium text-primary">铁路查询策略</p>
      {card.suggest_queries && card.suggest_queries.length > 0 && (
        <div>
          <p className="text-xs text-secondary">建议查询</p>
          <ul className="text-sm list-disc list-inside space-y-0.5">
            {card.suggest_queries.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </div>
      )}
      {card.why && card.why.length > 0 && (
        <div>
          <p className="text-xs text-secondary">为什么</p>
          <ul className="text-sm list-disc list-inside space-y-0.5">
            {card.why.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {card.presale?.map((p, i) => (
        <p key={i} className="text-xs text-secondary">
          起售：{p.route ?? p.city} {p.train_window}{" "}
          {p.open_at && (
            <FactField
              field={{ value: p.open_at, evidence: p.evidence ?? ESTIMATED_EVIDENCE }}
            />
          )}
        </p>
      ))}
      {card.official_entry?.url && (
        <div className="pt-1">
          <a
            href={card.official_entry.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center min-h-[44px] px-3 rounded-card border border-accent-blue
                       text-sm text-accent-blue hover:bg-accent-blue/10 transition-colors"
          >
            去 12306 官方查询 →
          </a>
          {card.official_entry.honest_note && (
            <p className="text-xs text-secondary mt-1">{card.official_entry.honest_note}</p>
          )}
        </div>
      )}
      {card.disclaimer && <p className="text-xs text-secondary">{card.disclaimer}</p>}
    </div>
  );
}

function FlightStrategy({ card }: { card: FlightStrategyCard }) {
  return (
    <div className="border border-border rounded-card p-2 space-y-1.5">
      <p className="text-sm font-medium text-primary">航班查询策略</p>
      {card.suggest_windows && card.suggest_windows.length > 0 && (
        <ul className="text-sm list-disc list-inside space-y-0.5">
          {card.suggest_windows.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {card.airport_compare && (
        <p className="text-xs text-secondary">
          机场比较：{card.airport_compare.origin?.join(" / ")} →{" "}
          {card.airport_compare.dest?.join(" / ")}
        </p>
      )}
      {card.schedules && card.schedules.length > 0 && (
        <ul className="text-xs space-y-0.5">
          {card.schedules.map((s) => (
            <li key={`${s.flight_no}-${s.dep_time}`} className="flex items-center gap-1 flex-wrap">
              <span className="text-primary">{s.flight_no}</span>
              <FactField
                field={{
                  value: `${s.dep_airport ?? ""} ${s.dep_time ?? ""} → ${s.arr_airport ?? ""} ${s.arr_time ?? ""}`,
                  evidence: s.evidence,
                }}
              />
            </li>
          ))}
        </ul>
      )}
      {card.checklist && card.checklist.length > 0 && (
        <p className="text-xs text-secondary">核对：{card.checklist.join("、")}</p>
      )}
      {card.price_entry_note && (
        <p className="text-xs text-secondary">{card.price_entry_note}</p>
      )}
    </div>
  );
}

function CandidateBlock({ origin, cand }: { origin?: string; cand: TransportCandidate }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <p className="font-medium text-primary">
          {origin ? `${origin} → ` : ""}
          {cand.city ?? "目的地"}
        </p>
        {cand.recommended_mode && (
          <Tag color="blue">{MODE_LABELS[cand.recommended_mode] ?? cand.recommended_mode}</Tag>
        )}
      </div>
      {cand.reason && <p className="text-sm text-secondary">{cand.reason}</p>}
      <div className="grid gap-2 sm:grid-cols-2">
        {cand.door_to_door?.rail && (
          <DoorToDoorBlock title="高铁门到门" d2d={cand.door_to_door.rail} />
        )}
        {cand.door_to_door?.air && (
          <DoorToDoorBlock title="飞机门到门" d2d={cand.door_to_door.air} />
        )}
      </div>
      {cand.rail_strategy && <RailStrategy card={cand.rail_strategy} />}
      {cand.flight_strategy && <FlightStrategy card={cand.flight_strategy} />}
    </div>
  );
}

export interface TransportCardProps {
  options: TransportOptions;
}

export function TransportCard({ options }: TransportCardProps) {
  const candidates = options.candidates ?? [];
  if (candidates.length === 0) return null;
  return (
    <div className="bg-card border border-border rounded-card p-3 space-y-3">
      <h3 className="font-medium text-primary">交通比较</h3>
      {candidates.map((cand, i) => (
        <CandidateBlock key={cand.city_code ?? cand.city ?? i} origin={options.origin} cand={cand} />
      ))}
      {options.disclaimer && (
        <p className="text-xs text-secondary border-t border-border pt-2">{options.disclaimer}</p>
      )}
    </div>
  );
}
