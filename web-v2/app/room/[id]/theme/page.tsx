// app/room/[id]/theme/page.tsx
// 主题选择页（DD-19 §5.3/PRD §7.3）：四种方式——直选 / 投票(ThemeVote) /
// AI 推荐（summary 偏好聚合 Top1）/ 转盘(ThemeWheel，服务端权威)。
// confirmTheme 后房间进入 RECOMMENDING → 跳 recommend 页。
// 说明：后端 confirm_theme 允许 COLLECTING 直接收敛（跳过显式 THEME_SELECTING），
// 故本页守卫状态为 COLLECTING + THEME_SELECTING。
"use client";

import { useCallback, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ApiError, confirmTheme, getRoomSummary } from "@/lib/api";
import { DEFAULT_THEMES } from "@/lib/constants";
import type { RoomSummary, VoteResponse } from "@/lib/types";
import { ThemeVote } from "@/components/room/ThemeVote";
import { ThemeWheel } from "@/components/room/ThemeWheel";
import { usePolling } from "@/components/room/usePolling";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";

type Method = "direct" | "vote" | "ai" | "wheel";

const METHOD_TABS: { value: Method; label: string }[] = [
  { value: "direct", label: "直接选" },
  { value: "vote", label: "大家投票" },
  { value: "ai", label: "AI 推荐" },
  { value: "wheel", label: "转盘" },
];

export default function RoomThemePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { room, session, loading, error } = useRoomGuard(id, [
    "COLLECTING",
    "THEME_SELECTING",
  ]);
  const [summary, setSummary] = useState<RoomSummary | null>(null);
  const [method, setMethod] = useState<Method>("direct");
  const [customTheme, setCustomTheme] = useState("");
  const [tally, setTally] = useState<VoteResponse["tally"]>([]);
  const [wheelTheme, setWheelTheme] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const pollSummary = useCallback(async () => {
    try {
      setSummary(await getRoomSummary(id));
    } catch {
      // 轮询失败静默
    }
  }, [id]);
  usePolling(pollSummary, 5000);

  const confirm = async (theme: string, m: Method) => {
    if (!theme.trim() || busy) return;
    setBusy(true);
    setNotice("");
    try {
      await confirmTheme(id, { theme: theme.trim(), method: m });
      router.push(`/room/${id}/recommend`);
    } catch (e) {
      setNotice(
        e instanceof ApiError && e.status === 409
          ? "房间状态已变化，正在刷新…"
          : e instanceof ApiError
            ? e.message
            : "确认主题失败，请重试",
      );
      setBusy(false);
    }
  };

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

  const candidates =
    summary?.theme_candidates?.length
      ? summary.theme_candidates
      : [...DEFAULT_THEMES];
  const aiPick = candidates[0] ?? null;

  return (
    <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl text-primary">这次玩什么主题？</h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date} · 四种方式任选其一，确认后开始推荐
        </p>
      </header>

      {/* 四种方式切换 */}
      <div className="flex gap-2">
        {METHOD_TABS.map((t) => (
          <Tag
            key={t.value}
            color="green"
            selected={method === t.value}
            onClick={() => setMethod(t.value)}
          >
            {t.label}
          </Tag>
        ))}
      </div>

      {notice && <p className="text-sm text-accent-red">{notice}</p>}

      {/* 直选 */}
      {method === "direct" && (
        <section className="bg-card border border-border rounded-card p-4 space-y-3">
          <div className="flex flex-wrap gap-2">
            {candidates.slice(0, 10).map((t) => (
              <Tag key={t} color="blue" onClick={() => confirm(t, "direct")}>
                {t}
              </Tag>
            ))}
          </div>
          <div className="flex gap-2">
            <Input
              value={customTheme}
              onChange={(e) => setCustomTheme(e.target.value)}
              placeholder="自定义主题，如：围炉煮茶"
              aria-label="自定义主题"
            />
            <Button
              variant="accent"
              onClick={() => confirm(customTheme, "direct")}
              disabled={busy || !customTheme.trim()}
            >
              直选
            </Button>
          </div>
        </section>
      )}

      {/* 投票 */}
      {method === "vote" && (
        <section className="space-y-3">
          <ThemeVote
            roomId={id}
            candidates={candidates.slice(0, 8)}
            memberToken={session?.member_token ?? null}
            onTally={setTally}
          />
          {tally.length > 0 && (
            <Button
              variant="accent"
              fullWidth
              disabled={busy}
              onClick={() => confirm(tally[0].theme, "vote")}
            >
              按投票结果确认：{tally[0].theme}（{tally[0].score} 分）
            </Button>
          )}
        </section>
      )}

      {/* AI 推荐 */}
      {method === "ai" && (
        <section className="bg-card border border-border rounded-card p-4 space-y-3">
          <p className="text-sm text-secondary">
            根据大家的兴趣与硬约束聚合，AI 建议：
          </p>
          {aiPick ? (
            <>
              <p className="text-lg font-semibold text-primary">{aiPick}</p>
              {summary && summary.conflicts.length > 0 && (
                <p className="text-xs text-accent-coral">
                  注意：{summary.conflicts.map((c) => `「${c.theme}」${c.reason}`).join("；")}
                </p>
              )}
              <Button
                variant="accent"
                fullWidth
                disabled={busy}
                onClick={() => confirm(aiPick, "ai")}
              >
                就听 AI 的
              </Button>
            </>
          ) : (
            <p className="text-sm text-secondary">暂无足够偏好数据，试试别的方式</p>
          )}
        </section>
      )}

      {/* 转盘（服务端权威结果，DD-19 §3.4） */}
      {method === "wheel" && (
        <section className="bg-card border border-border rounded-card p-4 space-y-3">
          <ThemeWheel
            roomId={id}
            candidates={candidates}
            onResult={setWheelTheme}
          />
          {wheelTheme && (
            <Button
              variant="accent"
              fullWidth
              disabled={busy}
              onClick={() => confirm(wheelTheme, "wheel")}
            >
              确认主题：{wheelTheme}
            </Button>
          )}
        </section>
      )}
    </main>
  );
}
