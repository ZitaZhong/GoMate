// app/plan/[id]/share/share-view.tsx
// 分享页视图（client）。脱敏白名单：只渲染 标题/摘要/目的地/主题/出发返程窗口/活动名，
// 一律不出现 精确地址、经纬度、个人预算与任何费用数字（DD-13 §7.3 / AGENTS.md 硬约束）。
"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, getPlanBundle, getPlanState } from "@/lib/api";
import type { TripBundle } from "@/lib/types";
import { FactField } from "@/components/evidence/FactField";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";

export function ShareView({ planId }: { planId: string }) {
  const [bundle, setBundle] = useState<TripBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const state = await getPlanState(planId);
        let b = (state.bundle ?? state.explore_bundle) as TripBundle | undefined;
        if (!b) {
          // state values 不含 bundle 大对象 → trip_bundles 表恢复（与 plan 页同一兜底）
          const persisted = await getPlanBundle(planId);
          b = (persisted.confirm ?? persisted.explore) as TripBundle | undefined;
        }
        setBundle(b && typeof b === "object" ? b : null);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "加载失败");
      } finally {
        setLoading(false);
      }
    })();
  }, [planId]);

  // 真实探索版契约（compose 顶层键）优先；旧嵌套 explore 兼容兜底
  const explore = bundle?.explore;
  const destination = bundle?.cities?.[0]?.name ?? explore?.destination;
  const theme = bundle?.theme ?? explore?.theme;
  const departWindow = bundle?.time_windows?.depart ?? explore?.depart_window;
  const returnWindow = bundle?.time_windows?.return ?? explore?.return_window;
  // 脱敏白名单：活动只展示名称（不展示精确地址/费用/来源）
  const activities =
    bundle?.activities?.slice(0, 5) ??
    (explore?.core_activities
      ? explore.core_activities.map((a) => ({ title: a.title?.value as string | undefined }))
      : undefined);
  const fmtWindow = (iso?: string) => {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? iso
      : d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric", weekday: "short" });
  };

  return (
    <main className="min-h-dvh bg-background flex flex-col items-center p-6">
      <div className="w-full max-w-md space-y-4">
        <header className="text-center space-y-1">
          <p className="font-handwrite text-2xl text-accent-green">GoMate</p>
          <p className="text-xs text-secondary">朋友分享了一个周末计划（已脱敏）</p>
        </header>

        {loading && <Skeleton lines={4} />}
        {error && <p className="text-sm text-accent-red text-center">{error}</p>}
        {!loading && !error && !bundle && (
          <p className="text-sm text-secondary text-center">这个计划还没有可分享的内容。</p>
        )}

        {bundle && (
          <div className="bg-card border border-border rounded-card p-4 space-y-3">
            {bundle.title && (
              <h1 className="text-lg font-medium text-primary text-center">{bundle.title}</h1>
            )}
            {bundle.summary && (
              <p className="text-sm text-secondary">{bundle.summary}</p>
            )}

            <div className="flex items-center justify-center gap-2 flex-wrap">
              {destination && (
                <span className="text-base text-primary">
                  {typeof destination === "string" ? destination : <FactField field={destination} />}
                </span>
              )}
              {theme && <Tag color="green">{theme}</Tag>}
            </div>

            {(departWindow || returnWindow) && (
              <dl className="text-sm space-y-1">
                {departWindow && (
                  <div className="flex gap-2">
                    <dt className="text-secondary shrink-0">出发</dt>
                    <dd>
                      {typeof departWindow === "string"
                        ? fmtWindow(departWindow)
                        : <FactField field={departWindow} />}
                    </dd>
                  </div>
                )}
                {returnWindow && (
                  <div className="flex gap-2">
                    <dt className="text-secondary shrink-0">返程</dt>
                    <dd>
                      {typeof returnWindow === "string"
                        ? fmtWindow(returnWindow)
                        : <FactField field={returnWindow} />}
                    </dd>
                  </div>
                )}
              </dl>
            )}

            {activities && activities.length > 0 && (
              <div>
                <p className="text-xs text-secondary mb-1">行程亮点</p>
                <ul className="text-sm space-y-1">
                  {activities.map((a, i) =>
                    a.title ? (
                      <li key={i}>
                        <FactField
                          field={{ value: a.title, evidence: (a as { evidence?: never }).evidence }}
                        />
                      </li>
                    ) : null,
                  )}
                </ul>
              </div>
            )}

            {bundle.disclaimer && (
              <p className="text-xs text-secondary border-t border-border pt-2">
                {bundle.disclaimer}
              </p>
            )}
          </div>
        )}

        <Link
          href="/chat"
          className="block w-full min-h-[44px] px-4 py-2 bg-accent-green text-white rounded-card
                     text-sm font-medium text-center hover:bg-accent-green/90 transition-colors"
        >
          也用 GoMate 规划我的周末 →
        </Link>
      </div>
    </main>
  );
}
