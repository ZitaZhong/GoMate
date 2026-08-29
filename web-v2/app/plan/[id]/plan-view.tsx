// app/plan/[id]/plan-view.tsx
// PlanView（client）：进页 GET /plans/{id}/state 渲染当前 bundle；
// 桌面左右分栏（Bundle | ChatPanel planId=当前 plan），移动端 Bundle 全屏 + 底部抽屉拉起 ChatPanel；
// bookings 回填表单（粘贴文本 → 识别结果用户确认 → POST bookings/import，BYO Booking）；
// ICS 日历订阅链接。
"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { ApiError, calendarIcsUrl, getPlanBundle, getPlanState, importBooking } from "@/lib/api";
import type { TripBundle } from "@/lib/types";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { PlanCard } from "@/components/cards/PlanCard";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";

type ImportStep =
  | { stage: "edit" }
  | { stage: "submitting" }
  | { stage: "confirm"; extracted: Record<string, unknown> }
  | { stage: "done" }
  | { stage: "error"; message: string };

const BOOKING_KINDS = [
  { value: "train", label: "火车票" },
  { value: "flight", label: "机票" },
  { value: "hotel", label: "酒店" },
] as const;

/** BYO Booking 回填表单：粘贴订单文本 → 展示识别结果 → 用户确认后落库。 */
function BookingImport({ planId }: { planId: string }) {
  const [kind, setKind] = useState<(typeof BOOKING_KINDS)[number]["value"]>("train");
  const [raw, setRaw] = useState("");
  const [step, setStep] = useState<ImportStep>({ stage: "edit" });

  const submitRaw = async () => {
    if (!raw.trim()) return;
    setStep({ stage: "submitting" });
    try {
      const resp = await importBooking(planId, { kind, input_kind: "text", raw: raw.trim() });
      if (resp.booking.confirmed) {
        setStep({ stage: "done" });
      } else {
        setStep({ stage: "confirm", extracted: resp.booking.extracted ?? {} });
      }
    } catch (e) {
      setStep({
        stage: "error",
        message: e instanceof ApiError ? e.message : "识别失败，请重试",
      });
    }
  };

  const confirmExtracted = async (extracted: Record<string, unknown>) => {
    setStep({ stage: "submitting" });
    try {
      const resp = await importBooking(planId, { kind, input_kind: "text", extracted });
      setStep(resp.booking.confirmed ? { stage: "done" } : { stage: "confirm", extracted });
    } catch (e) {
      setStep({
        stage: "error",
        message: e instanceof ApiError ? e.message : "确认失败，请重试",
      });
    }
  };

  return (
    <section id="booking" className="bg-card border border-border rounded-card p-3 space-y-2 scroll-mt-4">
      <div className="flex items-center gap-2">
        <h2 className="font-medium text-primary">回填你的订单</h2>
        <Tag color="neutral">BYO Booking</Tag>
      </div>
      <p className="text-xs text-secondary">
        我们不下单、不碰你的账户。在官方平台出票后，把订单文本粘贴到这里，确认识别结果即可。
      </p>

      {step.stage === "done" ? (
        <div className="space-y-2">
          <p className="text-sm text-primary flex items-center gap-2">
            <Tag color="green">已确认</Tag>
            订单已保存。可以在对话里说「票已出好」继续生成最终方案。
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setRaw("");
              setStep({ stage: "edit" });
            }}
          >
            再回填一笔
          </Button>
        </div>
      ) : (
        <>
          <Select
            label="订单类型"
            value={kind}
            onChange={(e) => setKind(e.target.value as typeof kind)}
          >
            {BOOKING_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </Select>
          <textarea
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            rows={4}
            placeholder="粘贴订单/出票短信全文，例如：G101 07-26 06:20 上海虹桥 → 北京南，二等座 ¥553"
            aria-label="订单文本"
            className="w-full px-3 py-2 bg-card border border-border rounded-card text-sm text-primary
                       placeholder:text-secondary/70 focus:outline-none focus:ring-2 focus:ring-accent-green/50"
          />
          {step.stage === "confirm" && (
            <div className="border border-accent-yellow/60 bg-accent-yellow/15 rounded-card p-2 space-y-1">
              <p className="text-xs font-medium text-secondary">识别结果，请逐项核对：</p>
              {Object.keys(step.extracted).length === 0 ? (
                <p className="text-sm text-secondary">未能识别出字段，请补充关键信息后再试。</p>
              ) : (
                <dl className="text-sm space-y-0.5">
                  {Object.entries(step.extracted).map(([k, v]) => (
                    <div key={k} className="flex gap-2">
                      <dt className="text-secondary shrink-0">{k}</dt>
                      <dd className="text-primary break-all">{String(v)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              <Button
                variant="accent"
                size="sm"
                disabled={Object.keys(step.extracted).length === 0}
                onClick={() => void confirmExtracted(step.extracted)}
              >
                确认无误，保存
              </Button>
            </div>
          )}
          {step.stage === "error" && (
            <p className="text-sm text-accent-red">{step.message}</p>
          )}
          <div className="flex gap-2">
            <Button
              variant="primary"
              onClick={() => void submitRaw()}
              disabled={!raw.trim() || step.stage === "submitting"}
            >
              {step.stage === "submitting" ? "识别中…" : "识别订单"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

function BundleColumn({
  planId,
  bundle,
  loading,
  error,
}: {
  planId: string;
  bundle: TripBundle | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <div className="space-y-3 p-4">
      <header className="flex items-center justify-between">
        <Link href="/" className="font-handwrite text-xl text-accent-green">
          GoMate
        </Link>
        <div className="flex items-center gap-2">
          <Link
            href={`/plan/${encodeURIComponent(planId)}/share`}
            className="inline-flex items-center min-h-[44px] text-sm text-secondary hover:text-primary"
          >
            分享
          </Link>
          <a
            href={calendarIcsUrl(planId)}
            className="inline-flex items-center min-h-[44px] text-sm text-accent-blue hover:underline"
          >
            订阅日历 ICS
          </a>
        </div>
      </header>

      {loading && <Skeleton lines={4} />}
      {error && <p className="text-sm text-accent-red">{error}</p>}
      {!loading && !error && !bundle && (
        <p className="text-sm text-secondary">
          方案还没有产出卡片。在右侧（移动端点下方「继续聊」）和 AI 聊聊，先聊出你的周末计划。
        </p>
      )}
      {bundle && <PlanCard bundle={bundle} />}
      {!loading && !error && <BookingImport planId={planId} />}
    </div>
  );
}

export function PlanView({ planId }: { planId: string }) {
  const [bundle, setBundle] = useState<TripBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const loadState = useCallback(async () => {
    try {
      const state = await getPlanState(planId);
      // state values 内：确认版 bundle（compose 写入）优先，探索版 explore_bundle 兜底；
      // state 不含 bundle 大对象时（interrupt/done 只落库），回退 trip_bundles 表恢复
      let b = (state.bundle ?? state.explore_bundle) as TripBundle | undefined;
      if (!b) {
        const persisted = await getPlanBundle(planId);
        b = (persisted.confirm ?? persisted.explore) as TripBundle | undefined;
      }
      setBundle(b && typeof b === "object" ? b : null);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "加载失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [planId]);

  // 初始加载（setState 只在 promise 回调中，onDone 重新拉取走 loadState）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const state = await getPlanState(planId);
        let b = (state.bundle ?? state.explore_bundle) as TripBundle | undefined;
        if (!b) {
          const persisted = await getPlanBundle(planId);
          b = (persisted.confirm ?? persisted.explore) as TripBundle | undefined;
        }
        if (cancelled) return;
        setBundle(b && typeof b === "object" ? b : null);
        setError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : "加载失败，请稍后重试");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [planId]);

  const column = (
    <BundleColumn planId={planId} bundle={bundle} loading={loading} error={error} />
  );

  return (
    <main className="h-dvh flex flex-col bg-background">
      {/* 桌面 ≥1024px：左右分栏 Bundle | ChatPanel */}
      <div className="hidden lg:grid lg:grid-cols-2 flex-1 min-h-0">
        <div className="overflow-y-auto border-r border-border">{column}</div>
        <div className="min-h-0">
          <ChatPanel planId={planId} onDone={() => void loadState()} />
        </div>
      </div>

      {/* 移动端：Bundle 全屏 + 底部「继续聊」抽屉 */}
      <div className="lg:hidden flex-1 min-h-0 overflow-y-auto pb-20">{column}</div>
      <div className="lg:hidden fixed inset-x-0 bottom-0 p-3 bg-card border-t border-border">
        <Button variant="accent" fullWidth onClick={() => setDrawerOpen(true)}>
          继续聊，修改方案
        </Button>
      </div>
      <AnimatePresence>
        {drawerOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="lg:hidden fixed inset-0 z-50 bg-black/30"
            onClick={() => setDrawerOpen(false)}
          >
            <motion.div
              role="dialog"
              aria-modal="true"
              aria-label="继续聊"
              initial={{ y: "100%" }}
              animate={{ y: 0 }}
              exit={{ y: "100%" }}
              transition={{ duration: 0.3 }}
              className="absolute inset-x-0 bottom-0 h-[85dvh] bg-card rounded-t-card
                         border-t border-border flex flex-col overflow-hidden"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between px-4 py-2 border-b border-border">
                <p className="font-medium text-primary">继续聊</p>
                <button
                  type="button"
                  aria-label="关闭"
                  onClick={() => setDrawerOpen(false)}
                  className="min-w-[44px] min-h-[44px] inline-flex items-center justify-center
                             text-secondary hover:text-primary"
                >
                  ✕
                </button>
              </div>
              <div className="flex-1 min-h-0">
                <ChatPanel planId={planId} onDone={() => void loadState()} />
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </main>
  );
}
