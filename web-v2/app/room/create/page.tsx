// app/room/create/page.tsx
// 创建房间（DD-19 §5.4）：POST /rooms → 保存创建者匿名会话 → 跳邀请页。
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, createRoom } from "@/lib/api";
import { saveRoomSession } from "@/lib/store";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

function nextSaturday(): string {
  const d = new Date();
  d.setDate(d.getDate() + (((6 - d.getDay()) + 7) % 7 || 7));
  return d.toISOString().slice(0, 10);
}

export default function RoomCreatePage() {
  const router = useRouter();
  const [form, setForm] = useState({
    activity_date: nextSaturday(),
    city: "上海",
    earliest: "10:00",
    latest: "21:00",
    budget_max: 200,
    creator_nickname: "",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!form.creator_nickname.trim()) {
      setError("请填写你的昵称");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const data = await createRoom({
        activity_date: form.activity_date,
        city: form.city.trim() || "上海",
        time_window: { earliest: form.earliest, latest: form.latest },
        budget_range: { min: 0, max: form.budget_max, currency: "CNY" },
        creator_nickname: form.creator_nickname.trim(),
      });
      // 创建者匿名会话（DD-19 §5.4）：member_token 仅放后续 POST/PUT body
      saveRoomSession(data.room_id, {
        member_id: data.member_id,
        member_token: data.member_token,
        nickname: form.creator_nickname.trim(),
      });
      router.push(`/room/${data.room_id}/invite`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "创建失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen bg-background flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-3">
        <h1 className="text-2xl text-center text-primary mb-1">
          <span className="font-handwrite text-accent-green">GoMate</span> 发起周末计划
        </h1>
        <p className="text-sm text-secondary text-center mb-4">
          创建房间，邀请朋友一起决定做什么
        </p>
        <Input
          label="活动日期"
          type="date"
          value={form.activity_date}
          onChange={(e) => setForm({ ...form, activity_date: e.target.value })}
        />
        <Input
          label="城市"
          value={form.city}
          onChange={(e) => setForm({ ...form, city: e.target.value })}
        />
        <div className="flex gap-2">
          <label className="flex-1 text-sm text-secondary">
            最早
            <input
              type="time"
              value={form.earliest}
              onChange={(e) => setForm({ ...form, earliest: e.target.value })}
              className="mt-1 w-full min-h-[44px] px-3 py-2 border border-border rounded-card bg-card text-sm text-primary"
            />
          </label>
          <label className="flex-1 text-sm text-secondary">
            最晚
            <input
              type="time"
              value={form.latest}
              onChange={(e) => setForm({ ...form, latest: e.target.value })}
              className="mt-1 w-full min-h-[44px] px-3 py-2 border border-border rounded-card bg-card text-sm text-primary"
            />
          </label>
        </div>
        <Input
          label="人均预算上限（元）"
          type="number"
          min={0}
          value={form.budget_max}
          onChange={(e) => setForm({ ...form, budget_max: Number(e.target.value) })}
        />
        <Input
          label="你的昵称"
          data-testid="creator-nickname"
          value={form.creator_nickname}
          onChange={(e) => setForm({ ...form, creator_nickname: e.target.value })}
          placeholder="例如：小北"
        />
        {error && <p className="text-sm text-accent-red">{error}</p>}
        <Button
          data-testid="create-room"
          variant="accent"
          fullWidth
          onClick={submit}
          disabled={busy}
        >
          {busy ? "创建中…" : "创建房间"}
        </Button>
      </div>
    </main>
  );
}
