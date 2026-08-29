// app/room/[id]/page.tsx
// 房间状态分发页（DD-19 §5.3）：GET /rooms/{id} → 按 status 重定向到子路由；
// EXPIRED 渲染只读页（全站操作禁用 + 「房间已过期」提示条）。
"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ApiError, getRoom, getRoomPlan } from "@/lib/api";
import { getRoomSession } from "@/lib/store";
import type { RoomDetailResponse, RoomPlanResponse, TimelineSlot } from "@/lib/types";
import { canonicalRoomPath } from "@/components/room/useRoomGuard";
import { usePolling } from "@/components/room/usePolling";
import { VerticalTimeline } from "@/components/itinerary/VerticalTimeline";
import { itineraryToSlots } from "@/components/itinerary/slotMapping";
import { Skeleton } from "@/components/ui/Skeleton";

export default function RoomDispatchPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [detail, setDetail] = useState<RoomDetailResponse | null>(null);
  const [plan, setPlan] = useState<RoomPlanResponse | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await getRoom(id);
      setDetail(d);
      if (d.room.status === "EXPIRED") {
        // 只读页尽力拉行程；404（未生成）不视为错误
        try {
          setPlan(await getRoomPlan(id));
        } catch {
          setPlan(null);
        }
      }
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 404
          ? "房间不存在或链接有误"
          : "加载房间失败，请稍后重试",
      );
    }
  }, [id]);

  // 一次性加载（不轮询；子页面各有守卫轮询）
  usePolling(load, 0);

  // 非 EXPIRED → 重定向到状态对应子路由（§5.3）
  useEffect(() => {
    if (!detail || detail.room.status === "EXPIRED") return;
    const session = getRoomSession(id);
    router.replace(
      canonicalRoomPath(id, detail.room.status, session, detail.members),
    );
  }, [detail, id, router]);

  if (error) {
    return (
      <main className="min-h-screen bg-background flex items-center justify-center p-6">
        <p className="text-sm text-accent-red">{error}</p>
      </main>
    );
  }

  if (!detail || detail.room.status !== "EXPIRED") {
    return (
      <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-3">
        <Skeleton className="h-8 w-1/2" />
        <Skeleton lines={3} />
      </main>
    );
  }

  // ---------------- EXPIRED 只读页 ----------------
  const { room, members } = detail;
  const slots: TimelineSlot[] = plan ? itineraryToSlots(plan.itinerary) : [];

  return (
    <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-4">
      <div className="bg-accent-red/15 border border-accent-red/30 rounded-card p-3">
        <p className="text-sm text-primary font-medium">房间已过期</p>
        <p className="text-xs text-secondary mt-0.5">
          以下内容仅供回顾，所有操作已禁用。
        </p>
      </div>

      <header className="space-y-1">
        <h1 className="text-2xl text-primary">
          {room.city} · {room.activity_date}
          {room.theme ? ` · ${room.theme}` : ""}
        </h1>
        <p className="text-sm text-secondary">
          {members.map((m) => m.nickname).join("、")}
        </p>
      </header>

      {slots.length > 0 ? (
        <section className="space-y-3 pointer-events-none opacity-90">
          <h2 className="text-sm font-medium text-primary">当时的行程</h2>
          <VerticalTimeline slots={slots} />
        </section>
      ) : (
        <p className="text-sm text-secondary">这个房间没有留下行程记录。</p>
      )}
    </main>
  );
}
