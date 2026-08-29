// app/room/[id]/member/page.tsx
// 成员信息填写页（DD-19 §5.5）：MemberForm 提交（updateMember 带 member_token）。
// 路由守卫：仅 COLLECTING；已提交成员可在此修改（表单回填）。
"use client";

import { useParams, useRouter } from "next/navigation";
import { MemberForm } from "@/components/room/MemberForm";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Skeleton } from "@/components/ui/Skeleton";

export default function RoomMemberPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { room, members, session, loading, error } = useRoomGuard(id, [
    "COLLECTING",
  ]);

  if (loading) {
    return (
      <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-3">
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

  const me = session
    ? members.find((m) => m.member_id === session.member_id)
    : undefined;

  return (
    <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl text-primary">
          {me?.submitted ? "修改我的信息" : "填写我的信息"}
        </h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date} · 信息只用于计算集合与通勤
        </p>
      </header>
      <MemberForm
        roomId={id}
        session={session}
        city={room.city}
        initial={me ?? null}
        onSubmitted={() => router.push(`/room/${id}/summary`)}
      />
    </main>
  );
}
