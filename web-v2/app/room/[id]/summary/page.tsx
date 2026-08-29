// app/room/[id]/summary/page.tsx
// 汇总页（DD-19 §3.5/§4.3）：便签墙 + AI 总结，5s 轮询 getRoomSummary
// （成员提交后便签 5s 内对其他人可见；窗口聚焦立即刷新）。
"use client";

import { useCallback, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getRoomSummary } from "@/lib/api";
import type { RoomSummary } from "@/lib/types";
import { MemberStickyWall, type StickySummary } from "@/components/room/MemberStickyWall";
import { usePolling } from "@/components/room/usePolling";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

export default function RoomSummaryPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  // 守卫（COLLECTING 专属）；成员列表来自守卫的 getRoom 轮询（含 hard_constraints）
  const { room, members, session, loading, error } = useRoomGuard(id, [
    "COLLECTING",
  ]);
  const [summary, setSummary] = useState<RoomSummary | null>(null);

  const pollSummary = useCallback(async () => {
    try {
      setSummary(await getRoomSummary(id));
    } catch {
      // 轮询失败静默，下一轮重试
    }
  }, [id]);
  usePolling(pollSummary, 5000);

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

  // 共同兴趣 = 已提交成员兴趣交集
  const submitted = members.filter((m) => m.submitted);
  const sharedInterests: string[] =
    submitted.length === 0
      ? []
      : [...submitted
          .map((m) => new Set(m.interests))
          .reduce((acc, s) => new Set([...acc].filter((t) => s.has(t))))];
  const cw = summary?.common_window;
  const stickySummary: StickySummary | null = summary
    ? {
        common_window: cw?.start
          ? `${cw.start} ~ ${cw.end}` +
            (cw.available_hours != null ? `（约 ${cw.available_hours} 小时）` : "")
          : "等待更多成员填写时间",
        shared_interests: [...sharedInterests],
        conflicts: [
          ...(summary.conflicts ?? []).map(
            (c) => `「${c.theme}」：${c.reason}`,
          ),
          ...(cw && !cw.feasible ? (cw.suggestions ?? []) : []),
        ],
      }
    : null;

  const meSubmitted = session
    ? members.find((m) => m.member_id === session.member_id)?.submitted
    : false;

  return (
    <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl text-primary">大家的想法</h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date} · 已提交{" "}
          {summary?.submitted_count ?? submitted.length}/{members.length} 人
          （5 秒自动刷新）
        </p>
      </header>

      <MemberStickyWall members={members} summary={stickySummary} />

      <div className="flex gap-2">
        {!meSubmitted && (
          <Button
            variant="secondary"
            fullWidth
            onClick={() => router.push(`/room/${id}/member`)}
          >
            去填写我的信息
          </Button>
        )}
        <Button
          variant="accent"
          fullWidth
          onClick={() => router.push(`/room/${id}/theme`)}
        >
          去决定主题
        </Button>
      </div>
    </main>
  );
}
