// app/room/[id]/recommend/page.tsx
// 推荐页（DD-19 §4.2/§4.3）：SSE 消费 GET /rooms/{id}/recommend——
// progress/research_progress → 进度；activity_candidates → FilterBar + ActivityCard 列表；
// interrupt → 候选就绪；done → 停止；error → 提示（ROOM_BUSY 提示稍候）。
// SSE 不可用（无 ReadableStream）时降级为轮询等待状态推进（§4.3）。
// selectActivity 后跳 activity/[aid]。
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ApiError, recommendUrl, selectActivity, backToTheme } from "@/lib/api";
import { consumeSSE } from "@/lib/sse";
import type { ActivityCandidate, RoomMember } from "@/lib/types";
import type { ActivityCardData } from "@/components/cards/ActivityCard";
import { FilterBar, type RecommendSort } from "@/components/cards/FilterBar";
import { ActivityList } from "@/components/room/ActivityList";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

const SSE_SUPPORTED =
  typeof window !== "undefined" && "ReadableStream" in window;

/** commute_times 与成员对齐：后端按「有坐标的成员」顺序给分钟数（rank_activities）；
 * 前端看不到坐标（出口脱敏），用 origin_name 近似；数量不匹配时降级为「成员 N」。 */
function zipCommutes(
  c: ActivityCandidate,
  members: RoomMember[],
): { nickname: string; minutes: number }[] | undefined {
  const times = c.commute_times;
  if (!times || times.length === 0) return undefined;
  const withOrigin = members.filter((m) => m.origin_name);
  if (times.length === withOrigin.length) {
    return times.map((t, i) => ({ nickname: withOrigin[i].nickname, minutes: t }));
  }
  if (times.length === members.length) {
    return times.map((t, i) => ({ nickname: members[i].nickname, minutes: t }));
  }
  return times.map((t, i) => ({ nickname: `成员 ${i + 1}`, minutes: t }));
}

function toCardData(c: ActivityCandidate, members: RoomMember[]): ActivityCardData {
  const ev = c.evidence;
  const maxCommute = c.commute_times?.length
    ? Math.max(...c.commute_times)
    : null;
  // 推荐理由：后端未提供文本时用真实排序字段合成（不虚构）
  const reasonParts: string[] = [];
  if (c.match_score != null) reasonParts.push(`综合匹配 ${Math.round(c.match_score * 100)}%`);
  if (maxCommute != null) reasonParts.push(`最远成员通勤约 ${maxCommute} 分钟`);
  return {
    id: String(c.id),
    title: { value: c.title, evidence: ev },
    venue: { value: c.venue, evidence: ev },
    start_at: { value: c.start_at, evidence: ev },
    price_text: { value: c.price_text, evidence: ev },
    booking_url: c.booking_url ? { value: c.booking_url, evidence: ev } : undefined,
    category: c.category,
    match_score: c.match_score,
    reason: reasonParts.join("，") || undefined,
    commute_times: zipCommutes(c, members),
    source_name: ev?.source_type,
    fetched_at: ev?.fetched_at,
  };
}

export default function RoomRecommendPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { room, members, loading, error } = useRoomGuard(id, ["RECOMMENDING"]);
  const [progress, setProgress] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<ActivityCandidate[]>([]);
  const [phase, setPhase] = useState<"idle" | "streaming" | "ready" | "failed">("idle");
  const [notice, setNotice] = useState("");
  const [sort, setSort] = useState<RecommendSort>("match");
  const [selectingId, setSelectingId] = useState<string | null>(null);
  const streaming = useRef(false);

  const startStream = useCallback(async () => {
    if (streaming.current) return;
    streaming.current = true;
    setPhase("streaming");
    setProgress([]);
    setNotice("");
    try {
      await consumeSSE(recommendUrl(id), {
        progress: (d) => {
          const m = d as { message?: string };
          if (m.message) setProgress((p) => [...p.slice(-8), m.message!]);
        },
        research_progress: (d) => {
          const m = d as { message?: string };
          if (m.message) setProgress((p) => [...p.slice(-8), m.message!]);
        },
        activity_candidates: (d) => {
          const list = (d as { candidates?: ActivityCandidate[] }).candidates ?? [];
          if (list.length) {
            setCandidates(list);
            setPhase("ready");
          }
        },
        interrupt: (d) => {
          const list =
            (d as { candidates?: ActivityCandidate[] }).candidates ?? [];
          if (list.length) {
            setCandidates(list);
            setPhase("ready");
          }
        },
        done: () => setPhase((p) => (p === "ready" ? p : "idle")),
        error: (d) => {
          const e = d as { code?: string; message?: string };
          setNotice(
            e.code === "ROOM_BUSY"
              ? "另一位成员正在生成推荐，请稍候片刻再刷新"
              : e.message ?? "推荐生成中断，请重试",
          );
          setPhase("failed");
        },
      });
      // 流正常结束但没有候选（如 0 个结果）
      setPhase((p) => (p === "streaming" ? "failed" : p));
    } catch {
      setNotice("推荐连接失败，请重试");
      setPhase("failed");
    } finally {
      streaming.current = false;
    }
  }, [id]);

  // 进入页面自动开始推荐流（SSE 不可用时降级：仅靠守卫轮询等状态被推进）
  // setTimeout 使 setState 发生在回调而非 effect 体内（lint 合规）
  useEffect(() => {
    if (!SSE_SUPPORTED || room?.status !== "RECOMMENDING") return;
    const t = setTimeout(() => void startStream(), 0);
    return () => clearTimeout(t);
  }, [room?.status, startStream]);

  const cards = useMemo(() => {
    const list = candidates.map((c) => ({ raw: c, card: toCardData(c, members) }));
    if (sort === "commute") {
      list.sort(
        (a, b) =>
          (a.raw.commute_fairness ?? 999) - (b.raw.commute_fairness ?? 999),
      );
    } else {
      list.sort((a, b) => (b.raw.match_score ?? 0) - (a.raw.match_score ?? 0));
    }
    return list.map((x) => x.card);
  }, [candidates, members, sort]);

  const select = async (card: ActivityCardData) => {
    if (selectingId) return;
    setSelectingId(card.id);
    setNotice("");
    try {
      await selectActivity(id, { activity_id: card.id });
      router.push(`/room/${id}/activity/${card.id}`);
    } catch (e) {
      setNotice(
        e instanceof ApiError && e.status === 409
          ? "房间正在规划中或状态已变化，正在刷新…"
          : "选定失败，请重试",
      );
      setSelectingId(null);
    }
  };

  // 换一个类似活动（PRD §7.4.7）：把该候选移到列表末尾
  const swap = (card: ActivityCardData) => {
    setCandidates((prev) => {
      const idx = prev.findIndex((c) => String(c.id) === card.id);
      if (idx < 0) return prev;
      return [...prev.slice(0, idx), ...prev.slice(idx + 1), prev[idx]];
    });
  };

  // 回退到主题选择（推荐页空结果时换主题）
  const handleBackToTheme = async () => {
    try {
      await backToTheme(id);
      router.push(`/room/${id}/theme`);
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "回退失败，请重试");
    }
  };

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

  return (
    <main className="min-h-screen bg-background p-6 max-w-2xl mx-auto space-y-4">
      <header className="space-y-1">
        <h1 className="text-xl text-primary">为你推荐活动</h1>
        <p className="text-sm text-secondary">
          {room.city} · {room.activity_date}
          {room.theme ? ` · 主题：${room.theme}` : ""}
        </p>
      </header>

      {!SSE_SUPPORTED && (
        <p className="text-xs text-secondary bg-accent-yellow/15 rounded-card p-2">
          当前浏览器不支持实时推荐流，页面会自动刷新等待结果。
        </p>
      )}

      {/* 进度 */}
      {phase === "streaming" && (
        <section className="bg-card border border-border rounded-card p-4 space-y-1">
          <p className="text-sm text-primary">AI 正在检索与研究…</p>
          <ul className="text-xs text-secondary space-y-0.5">
            {progress.map((p, i) => (
              <li key={`${i}-${p}`}>· {p}</li>
            ))}
          </ul>
        </section>
      )}

      {notice && (
        <div className="bg-accent-red/15 border border-accent-red/30 rounded-card p-3 flex items-center justify-between gap-2">
          <p className="text-sm text-primary">{notice}</p>
          {phase === "failed" && SSE_SUPPORTED && (
            <Button size="sm" variant="secondary" onClick={startStream}>
              重试
            </Button>
          )}
        </div>
      )}

      {/* 候选列表 */}
      {cards.length > 0 && (
        <>
          <FilterBar
            conditions={{
              city: room.city,
              activityDate: room.activity_date,
              theme: room.theme,
              budgetMaxYuan: room.budget_range?.max ?? null,
            }}
            sort={sort}
            onSortChange={setSort}
          />
          <ActivityList
            activities={cards}
            selectingId={selectingId}
            onSelect={select}
            onSwap={swap}
            onDetail={(a) => router.push(`/room/${id}/activity/${a.id}`)}
          />
        </>
      )}

      {phase === "failed" && cards.length === 0 && !notice && (
        <div className="space-y-3">
          <p className="text-sm text-secondary">
            没有找到符合条件的活动，可以回上一步换个主题试试。
          </p>
          <Button variant="secondary" onClick={handleBackToTheme}>
            回上一步换主题
          </Button>
        </div>
      )}
    </main>
  );
}
