// app/room/[id]/invite/page.tsx
// 邀请页：① 已有成员身份 → 邀请链接/邀请码展示（InviteCard 风格，可复制）；
// ② 新成员带 ?code= 进入 → by-invite 解析 → 填昵称 → joinRoom → saveRoomSession → 成员页。
// 路由守卫：仅 DRAFT/COLLECTING 服务本页（邀请在收集期有效），其余状态按 §5.3 重定向。
"use client";

import { Suspense, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ApiError, getRoomByInvite, joinRoom } from "@/lib/api";
import { saveRoomSession } from "@/lib/store";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";

function InviteInner() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const code = searchParams.get("code") ?? "";
  const { room, members, session, loading, error } = useRoomGuard(id, [
    "DRAFT",
    "COLLECTING",
  ]);

  const [origin] = useState(() =>
    typeof window === "undefined" ? "" : window.location.origin,
  );
  const [copied, setCopied] = useState<"link" | "code" | null>(null);

  const copy = async (text: string, kind: "link" | "code") => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // clipboard 不可用时退化为选中文本
      window.prompt("复制以下内容：", text);
    }
    setCopied(kind);
    setTimeout(() => setCopied(null), 2000);
  };

  if (loading) {
    return (
      <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-3">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton lines={3} />
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

  // 新成员（带 code 且当前用户不是创建者）→ 加入表单
  // 注意：同一浏览器新窗口共享 localStorage，需通过 creator_member_id 精确判断身份
  const isCreator = session && room.creator_member_id === session.member_id;
  if (code && !isCreator) {
    return <JoinByCode roomId={id} code={code} expectedId={room.id} />;
  }

  const inviteUrl = `${origin}/room/${id}/invite?code=${room.invite_code}`;

  return (
    <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-4">
      <header className="text-center space-y-1">
        <h1 className="text-xl text-primary">
          <span className="font-handwrite text-2xl text-accent-green">GoMate</span>{" "}
          邀请朋友加入
        </h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date}，一起决定周末做什么
        </p>
      </header>

      {/* 邀请卡（InviteCard 风格） */}
      <section className="bg-card border border-border rounded-card p-6 text-center space-y-4 shadow-sm">
        <p className="text-sm text-secondary">邀请码</p>
        <p
          data-testid="invite-code"
          className="text-3xl tracking-widest font-mono text-primary"
        >
          {room.invite_code}
        </p>
        <div className="space-y-2">
          <Button
            variant="accent"
            fullWidth
            onClick={() => copy(inviteUrl, "link")}
          >
            {copied === "link" ? "已复制链接" : "复制邀请链接"}
          </Button>
          <Button
            variant="secondary"
            fullWidth
            onClick={() => copy(room.invite_code, "code")}
          >
            {copied === "code" ? "已复制邀请码" : "复制邀请码"}
          </Button>
        </div>
        <p className="text-xs text-secondary break-all">{inviteUrl}</p>
      </section>

      {/* 已加入成员 */}
      <section className="bg-card border border-border rounded-card p-4">
        <p className="text-sm font-medium text-primary mb-2">
          已加入（{members.length}）
        </p>
        <ul className="text-sm text-secondary space-y-1">
          {members.map((m) => (
            <li key={m.member_id}>
              {m.nickname}
              {m.submitted ? " · 已填信息" : " · 待填写"}
            </li>
          ))}
        </ul>
      </section>

      <Button
        variant="primary"
        fullWidth
        onClick={() => router.push(`/room/${id}/member`)}
      >
        {session ? "去填写我的信息" : "我是发起人，去填写信息"}
      </Button>
    </main>
  );
}

/** 新成员经邀请链接加入：校验 code → 昵称 → joinRoom → saveRoomSession */
function JoinByCode({
  roomId,
  code,
  expectedId,
}: {
  roomId: string;
  code: string;
  expectedId: number;
}) {
  const router = useRouter();
  const [nickname, setNickname] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const join = async () => {
    if (!nickname.trim()) {
      setError("请填写你的昵称");
      return;
    }
    setBusy(true);
    setError("");
    try {
      // 以 ?code= 为准重新解析房间（防链接里的 id 与 code 不匹配）
      const { room } = await getRoomByInvite(code);
      if (String(room.id) !== String(expectedId)) {
        router.replace(`/room/${room.id}/invite?code=${code}`);
        return;
      }
      const data = await joinRoom(room.id, { nickname: nickname.trim() });
      saveRoomSession(room.id, {
        member_id: data.member_id,
        member_token: data.member_token,
        nickname: nickname.trim(),
      });
      router.push(`/room/${roomId}/member`);
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
        <h1 className="text-xl text-center text-primary">加入这个周末计划</h1>
        <p className="text-sm text-secondary text-center">
          填写昵称即可加入，无需注册
        </p>
        <Input
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder="你的昵称"
          aria-label="昵称"
          onKeyDown={(e) => e.key === "Enter" && join()}
        />
        {error && <p className="text-sm text-accent-red">{error}</p>}
        <Button variant="accent" fullWidth onClick={join} disabled={busy}>
          {busy ? "加入中…" : "加入房间"}
        </Button>
      </div>
    </main>
  );
}

export default function RoomInvitePage() {
  return (
    <Suspense>
      <InviteInner />
    </Suspense>
  );
}
