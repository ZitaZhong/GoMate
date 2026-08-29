// app/room/join/page.tsx
// 加入房间（DD-19 §5.4）：邀请码 → by-invite 解析 → 填昵称 → joinRoom →
// saveRoomSession → 跳成员信息页。成员信息填写收敛到 /room/[id]/member。
"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ApiError, getRoomByInvite, joinRoom } from "@/lib/api";
import { saveRoomSession } from "@/lib/store";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

function JoinInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [code, setCode] = useState(params.get("code") ?? "");
  const [nickname, setNickname] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const join = async () => {
    if (!code.trim() || !nickname.trim()) {
      setError("请填写邀请码和昵称");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const { room } = await getRoomByInvite(code.trim());
      const data = await joinRoom(room.id, { nickname: nickname.trim() });
      saveRoomSession(room.id, {
        member_id: data.member_id,
        member_token: data.member_token,
        nickname: nickname.trim(),
      });
      router.push(`/room/${room.id}/member`);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 404
          ? "邀请码无效"
          : e instanceof ApiError
            ? e.message
            : "加入失败，请重试",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen bg-background flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-3">
        <h1 className="text-xl text-center text-primary mb-2">加入周末计划</h1>
        <Input
          data-testid="invite-code-input"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="邀请码"
          aria-label="邀请码"
        />
        <Input
          data-testid="nickname-input"
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder="你的昵称"
          aria-label="昵称"
          onKeyDown={(e) => e.key === "Enter" && join()}
        />
        {error && <p className="text-sm text-accent-red">{error}</p>}
        <Button
          data-testid="join-room"
          variant="accent"
          fullWidth
          onClick={join}
          disabled={busy}
        >
          {busy ? "加入中…" : "加入房间"}
        </Button>
      </div>
    </main>
  );
}

export default function RoomJoinPage() {
  return (
    <Suspense>
      <JoinInner />
    </Suspense>
  );
}
