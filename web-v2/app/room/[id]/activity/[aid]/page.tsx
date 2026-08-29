// app/room/[id]/activity/[aid]/page.tsx
// 活动详情页：活动信息（行程中的 activity 节点，FactField 渲染）+
// 各成员路线（getRoomRoutes + CommuteBar 最远高亮）+ 集合信息（GatheringInfo）+
// 共同决策入口（去行程页调整）。
// 说明：select-activity 后端同步跑完 gathering→itinerary→publish，
// 到达本页时状态可能已是 PLANNING/PUBLISHED，守卫三种状态都放行。
"use client";

import { useCallback, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getRoomPlan, getRoomRoutes } from "@/lib/api";
import type { ItineraryNode, RoomPlanResponse, RoomRoutesResponse } from "@/lib/types";
import { CommuteBar } from "@/components/cards/CommuteBar";
import { GatheringInfo } from "@/components/room/GatheringInfo";
import { FactField } from "@/components/evidence/FactField";
import { usePolling } from "@/components/room/usePolling";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

export default function RoomActivityPage() {
  const { id, aid } = useParams<{ id: string; aid: string }>();
  const router = useRouter();
  const { room, loading, error } = useRoomGuard(id, [
    "ACTIVITY_SELECTED",
    "PLANNING",
    "PUBLISHED",
  ]);
  const [routes, setRoutes] = useState<RoomRoutesResponse | null>(null);
  const [plan, setPlan] = useState<RoomPlanResponse | null>(null);

  const load = useCallback(async () => {
    try {
      setRoutes(await getRoomRoutes(id));
    } catch {
      // 路线未就绪时静默，轮询重试
    }
    try {
      setPlan(await getRoomPlan(id));
    } catch {
      // PLANNING 中行程可能未生成（404），静默
    }
  }, [id]);

  // 路线/行程 5s 轮询（PLANNING 中可能未生成，404 静默重试）
  usePolling(load, 5000);

  if (loading) {
    return (
      <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-3">
        <Skeleton className="h-8 w-1/2" />
        <Skeleton lines={4} />
      </main>
    );
  }
  if (error || !room) {
    return (
      <main className="min-h-screen bg-background flex items-center justify-center p-6">
        <p className="text-sm text-accent-red">{error ?? "加载失败"}</p>
      </main>
    );
  }

  const activityNode: ItineraryNode | undefined = plan?.itinerary.nodes.find(
    (n) => n.type === "activity",
  );
  const memberRoutes = routes?.member_routes ?? [];
  const maxDuration = memberRoutes.reduce((m, r) => Math.max(m, r.duration_min), 0);

  return (
    <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl text-primary">活动详情</h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date}
          {room.theme ? ` · 主题：${room.theme}` : ""}
        </p>
      </header>

      {/* 活动信息（行程 activity 节点，事实字段走 FactField） */}
      <section className="bg-card border border-border rounded-card p-4 space-y-2" data-aid={aid}>
        {activityNode ? (
          <>
            <h2 className="font-semibold text-primary">
              <FactField
                field={{ value: activityNode.title, evidence: activityNode.evidence }}
              />
            </h2>
            {activityNode.venue && (
              <p className="text-sm text-secondary">
                <FactField
                  field={{ value: activityNode.venue, evidence: activityNode.evidence }}
                />
              </p>
            )}
            <div className="flex flex-wrap gap-3 text-sm text-secondary">
              {activityNode.start && (
                <FactField
                  field={{ value: activityNode.start, evidence: activityNode.evidence }}
                  render={(v) => formatTime(v as string)}
                />
              )}
            </div>
            {activityNode.note && (
              <p className="text-xs text-secondary">{activityNode.note}</p>
            )}
            {activityNode.booking_url && (
              <a
                href={activityNode.booking_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center min-h-[44px] px-3 rounded-lg border border-border
                           text-sm text-secondary hover:bg-background"
              >
                查看官方页面
              </a>
            )}
          </>
        ) : (
          <p className="text-sm text-secondary">
            活动信息生成中…（页面每 5 秒自动刷新）
          </p>
        )}
      </section>

      {/* 集合信息 */}
      <GatheringInfo gathering={routes?.gathering ?? plan?.itinerary.gathering ?? null} />

      {/* 各成员路线（最远高亮） */}
      {memberRoutes.length > 0 && (
        <section className="bg-card border border-border rounded-card p-4 space-y-2">
          <h3 className="text-sm font-medium text-primary">各成员路线</h3>
          <div className="space-y-1.5">
            {memberRoutes.map((r) => (
              <div key={r.member_id} className="space-y-0.5">
                <CommuteBar
                  nickname={r.nickname}
                  minutes={r.duration_min}
                  max={maxDuration}
                />
                <p className="text-xs text-secondary pl-16">
                  {r.transport_mode}
                  {r.estimate ? "（估算）" : ""}
                  {r.note ? ` · ${r.note}` : ""}
                  {r.deeplink && (
                    <>
                      {" · "}
                      <a
                        href={r.deeplink}
                        target="_blank"
                        rel="noreferrer"
                        className="text-accent-blue underline"
                      >
                        导航
                      </a>
                    </>
                  )}
                </p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* 共同决策入口 */}
      <div className="flex gap-2">
        <Button
          variant="accent"
          fullWidth
          onClick={() => router.push(`/room/${id}/plan`)}
        >
          去看完整行程 / 一起调整
        </Button>
        {room.status === "PUBLISHED" && (
          <Button
            variant="secondary"
            fullWidth
            onClick={() => router.push(`/room/${id}/share`)}
          >
            生成分享卡
          </Button>
        )}
      </div>
    </main>
  );
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso.slice(0, 16);
  }
}
