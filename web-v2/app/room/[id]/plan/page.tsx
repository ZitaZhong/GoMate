// app/room/[id]/plan/page.tsx
// 行程页：getRoomPlan 渲染 VerticalTimeline + BudgetSummary + ModifyInput
// （POST /rooms/{id}/plan/modify SSE 流式修改：revision_classified /
// needs_confirmation → 二次确认 / no_change / itinerary_updated → done 后重拉 plan）。
// undoRoomPlan 撤销按钮；PUBLISHED 状态显示分享入口。
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  ApiError,
  getRoomPlan,
  getRoomSummary,
  modifyPlanUrl,
  undoRoomPlan,
} from "@/lib/api";
import { consumeSSE } from "@/lib/sse";
import type { RoomPlanResponse, TimelineSlot } from "@/lib/types";
import { BudgetSummary } from "@/components/itinerary/BudgetSummary";
import { ModifyInput } from "@/components/itinerary/ModifyInput";
import { VerticalTimeline } from "@/components/itinerary/VerticalTimeline";
import { itineraryToSlots } from "@/components/itinerary/slotMapping";
import { usePolling } from "@/components/room/usePolling";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

const REVISION_LABEL: Record<string, string> = {
  replace_node: "替换节点",
  add_node: "新增节点",
  remove_node: "删除节点",
  adjust_time: "调整时间",
  adjust_budget: "调整预算",
  change_transport: "调整出行方式",
  change_theme: "更换主题",
  full_replan: "整体重排",
};

export default function RoomPlanPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { room, loading, error } = useRoomGuard(id, ["PLANNING", "PUBLISHED"]);
  const [plan, setPlan] = useState<RoomPlanResponse | null>(null);
  const [planMissing, setPlanMissing] = useState(false);
  const [budgetMin, setBudgetMin] = useState<number | null>(null);
  const [notice, setNotice] = useState("");
  const [classifyInfo, setClassifyInfo] = useState("");
  const [pendingReasons, setPendingReasons] = useState<string[] | null>(null);
  const [prefill, setPrefill] = useState("");
  const [streaming, setStreaming] = useState(false);
  const lastMessage = useRef("");

  const loadPlan = useCallback(async () => {
    try {
      setPlan(await getRoomPlan(id));
      setPlanMissing(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setPlanMissing(true);
    }
  }, [id]);

  // PLANNING 中行程可能未生成：5s 轮询直到出现
  usePolling(loadPlan, 5000);

  // 成员最低预算（预算约束摘要；一次性拉取）
  useEffect(() => {
    if (!room) return;
    let cancelled = false;
    getRoomSummary(id)
      .then((s) => {
        if (!cancelled) setBudgetMin(s.budget_min);
      })
      .catch(() => {
        if (!cancelled) setBudgetMin(null);
      });
    return () => {
      cancelled = true;
    };
  }, [room, id]);

  const modify = async (message: string, confirm = false) => {
    if (streaming) return;
    setStreaming(true);
    setNotice("");
    setClassifyInfo("");
    if (!confirm) {
      lastMessage.current = message;
      setPendingReasons(null);
    }
    try {
      await consumeSSE(
        modifyPlanUrl(id),
        {
          revision_classified: (d) => {
            const dec = d as { revision_type?: string };
            if (dec.revision_type) {
              setClassifyInfo(
                `理解为：${REVISION_LABEL[dec.revision_type] ?? dec.revision_type}`,
              );
            }
          },
          needs_confirmation: (d) => {
            setPendingReasons((d as { reasons?: string[] }).reasons ?? []);
          },
          no_change: (d) => {
            setNotice((d as { message?: string }).message ?? "没有可修改的节点");
          },
          itinerary_updated: () => {
            setPendingReasons(null);
            void loadPlan();
          },
          done: () => setStreaming(false),
          error: (d) => {
            const e = d as { code?: string; message?: string };
            setNotice(
              e.code === "ROOM_BUSY"
                ? "另一位成员正在修改，请稍候"
                : e.message ?? "修改失败，行程保持原样",
            );
          },
        },
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, confirm }),
        },
      );
    } catch {
      setNotice("修改请求失败，请重试");
    } finally {
      setStreaming(false);
    }
  };

  const undo = async () => {
    setNotice("");
    try {
      const resp = await undoRoomPlan(id);
      setPlan(resp);
    } catch (e) {
      setNotice(
        e instanceof ApiError && e.status === 404
          ? "没有可撤销的历史版本"
          : "撤销失败，请重试",
      );
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

  const slots: TimelineSlot[] = plan ? itineraryToSlots(plan.itinerary) : [];

  return (
    <main className="min-h-screen bg-background max-w-2xl mx-auto flex flex-col">
      <div className="flex-1 p-6 space-y-4 pb-8">
        <header className="flex items-start justify-between gap-2">
          <div className="space-y-1">
            <h1 className="text-xl text-primary">
              行程安排
              {plan?.version != null && (
                <span className="text-xs text-secondary ml-2">v{plan.version}</span>
              )}
            </h1>
            <p className="text-sm text-secondary">
              {room.city} · {room.activity_date}
              {room.theme ? ` · ${room.theme}` : ""}
            </p>
          </div>
          <div className="flex gap-2 shrink-0">
            <Button size="sm" variant="secondary" onClick={undo} disabled={streaming}>
              撤销
            </Button>
            {room.status === "PUBLISHED" && (
              <Button
                size="sm"
                variant="accent"
                onClick={() => router.push(`/room/${id}/share`)}
              >
                分享
              </Button>
            )}
          </div>
        </header>

        {planMissing && (
          <p className="text-sm text-secondary bg-card border border-border rounded-card p-3">
            行程生成中…（页面每 5 秒自动刷新）
          </p>
        )}

        {plan && (
          <>
            <BudgetSummary
              budgetRange={room.budget_range}
              minMemberBudget={budgetMin}
              commonWindow={plan.itinerary.common_time_window ?? null}
              warnings={plan.itinerary.warnings ?? []}
            />
            <VerticalTimeline
              slots={slots}
              onNodeAction={(slot, action) =>
                setPrefill(`${action}「${slot.title}」：`)
              }
            />
          </>
        )}

        {classifyInfo && (
          <p className="text-xs text-secondary">AI {classifyInfo}</p>
        )}
        {pendingReasons && (
          <div className="text-sm text-primary border border-accent-yellow bg-accent-yellow/15 rounded-card p-3 space-y-2">
            <p>这次修改需要确认：{pendingReasons.join("；")}</p>
            <Button
              size="sm"
              variant="secondary"
              disabled={streaming}
              onClick={() => modify(lastMessage.current, true)}
            >
              确认执行
            </Button>
          </div>
        )}
        {notice && <p className="text-sm text-accent-red">{notice}</p>}
        {streaming && (
          <p className="text-xs text-secondary">AI 正在修改行程…</p>
        )}
      </div>

      {/* 底部固定修改输入（DD-19 §3.7） */}
      <ModifyInput
        onSubmit={(text) => void modify(text)}
        disabled={streaming || !plan}
        prefill={prefill}
      />
    </main>
  );
}
