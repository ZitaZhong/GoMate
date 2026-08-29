// components/chat/ChatPanel.tsx
// 对话 Copilot 面板（v4 回合状态机，/agent/* 全量对接）：
//   POST /agent/conversations/{pid}/turns（幂等键）→ AgentTurnResponse；
//   d.run → EventSource(events_url) 订阅 run 事件流（断线自动重连携 Last-Event-ID 续传）；
//   research.progress/run.status/run.node → 进度条；run.error → 警示气泡；
//   payload.final → GET workspace → 探索版卡片 + 终态回复 + onDone；
//   d.clarification → 澄清卡（blocking=需回答 / 非阻塞=可选补充，直接输入框回复）；
//   d.route_plan → 路线卡（design_itinerary 同步作答，DD-15 v1.1）。
// pid="new" 时首条消息由服务端自动建 plan，收到 plan_id 后
// setPlanId + 原生 history.replaceState 更新 URL（避免 key 重挂载清空会话）；
// 数字 planId 挂载时经 workspace 恢复会话/运行中任务/待澄清（v4 §11.3，刷新不丢上下文）。
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { MessageBubble } from "./MessageBubble";
import { QuickCommands } from "./QuickCommands";
import { ResearchProgress } from "./ResearchProgress";
import {
  agentRunEventsUrl,
  agentTurn,
  cancelAgentRun,
  getAgentWorkspace,
} from "@/lib/api";
import type {
  AgentClarification,
  AgentRunEvent,
  AgentRunView,
  AgentTurnResponse,
  CardPayload,
  ChatMessage,
} from "@/lib/types";
import { Button } from "@/components/ui/Button";

interface ResearchPhase {
  phase: string;
  message?: string;
}

export interface ChatPanelProps {
  planId?: string;
  /** run 终态（方案更新）后回调（计划页用来刷新 bundle 视图） */
  onDone?: () => void;
}

/** 澄清卡文案：问题 + 原因 + 阻塞语义说明（对齐 v4 参照实现 web/index.html）。 */
function clarificationText(clar: AgentClarification): string {
  const head = clar.blocking ? `❓ 需要你确认：${clar.question}` : `💡 可选补充：${clar.question}`;
  const parts = [head];
  if (clar.reason) parts.push(`原因：${clar.reason}`);
  const skips = (clar.assumptions_if_skipped ?? []).join("；");
  parts.push(
    clar.blocking
      ? "回答后我会从当前上下文继续，直接在下方输入框回复即可。"
      : skips
        ? `不回答也会继续；默认假设：${skips}。`
        : "不回答也会继续执行，补充后可以优化结果。",
  );
  return parts.join("\n");
}

export function ChatPanel({ planId: initialPlanId, onDone }: ChatPanelProps) {
  const [planId, setPlanId] = useState(initialPlanId ?? "new");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [activeRun, setActiveRun] = useState<AgentRunView | null>(null);
  const [researchPhase, setResearchPhase] = useState<ResearchPhase | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);
  const finishedRef = useRef(false);

  const newMessage = (role: ChatMessage["role"], content: string): ChatMessage => ({
    id: crypto.randomUUID(),
    role,
    content,
    timestamp: new Date().toISOString(),
  });

  const pushAssistant = useCallback(
    (content: string, cards?: CardPayload[]) =>
      setMessages((prev) => [...prev, { ...newMessage("assistant", content), cards }]),
    [],
  );

  const closeRunSource = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
  }, []);

  /** run 终态：关流 → workspace 拉全量状态 → 方案卡 + 终态回复 + onDone。 */
  const finishRun = useCallback(
    async (pid: string) => {
      if (finishedRef.current) return;
      finishedRef.current = true;
      closeRunSource();
      setActiveRun(null);
      setResearchPhase(null);
      try {
        const ws = await getAgentWorkspace(pid);
        const turn = ws.active_turn;
        const reply =
          turn?.visible_reply &&
          ["answered", "partial", "failed", "cancelled"].includes(turn.status)
            ? turn.visible_reply
            : "";
        // 方案卡挂在终态回复气泡下（explore_bundle 契约，CardRouter 路由）
        pushAssistant(
          reply || "本轮任务已结束，最新方案见下方卡片。",
          ws.current_plan ? [{ data: { explore_bundle: ws.current_plan } }] : undefined,
        );
        for (const clar of ws.open_clarifications ?? []) {
          pushAssistant(clarificationText(clar));
        }
      } catch {
        pushAssistant("任务已结束，但拉取最新方案失败——刷新页面可恢复完整工作区。");
      }
      onDone?.();
    },
    [closeRunSource, onDone, pushAssistant],
  );

  /** 订阅 run 事件流：EventSource 断线自动重连并携 Last-Event-ID 续传（v4 §6.5）。 */
  const subscribeRun = useCallback(
    (run: AgentRunView, pid: string, after = 0) => {
      closeRunSource();
      finishedRef.current = false;
      setActiveRun(run);
      setResearchPhase({
        phase: "research",
        message: run.status === "queued" ? "任务已创建，等待执行" : "任务执行中",
      });
      const es = new EventSource(agentRunEventsUrl(run, after));
      esRef.current = es;
      const handle = (e: MessageEvent) => {
        let d: AgentRunEvent = {};
        try {
          d = JSON.parse(e.data) as AgentRunEvent;
        } catch {
          return;
        }
        const kind = d.type ?? e.type;
        if (kind === "research.progress") {
          const total = d.payload?.progress?.total ?? 0;
          const done = d.payload?.progress?.completed ?? 0;
          const suffix = total ? `（${done}/${total}）` : "";
          setResearchPhase({
            phase: d.phase ?? "research",
            message: `${d.message ?? ""}${suffix}`,
          });
        } else if (kind === "run.error") {
          pushAssistant(`⚠ ${d.message ?? "执行出错（已保留可用结果）"}`);
        } else if (d.message || d.phase) {
          setResearchPhase({ phase: d.phase ?? "research", message: d.message });
        }
        if (d.payload?.final || d.final) void finishRun(pid);
      };
      for (const t of ["research.progress", "run.status", "run.result", "run.error", "run.node"]) {
        es.addEventListener(t, handle as EventListener);
      }
      es.onerror = () => {
        /* 服务端持有生命周期；EventSource 自动重连并续传 */
      };
    },
    [closeRunSource, finishRun, pushAssistant],
  );

  /** 取消运行中任务（v4：取消是一等操作，进度区常驻取消按钮）。 */
  const cancelRun = useCallback(async () => {
    if (!activeRun) return;
    try {
      await cancelAgentRun(activeRun.id);
      // 终态事件（cancelled）会经事件流回来触发 finishRun；这里只做即时反馈
      setResearchPhase({ phase: "research", message: "正在取消任务…" });
    } catch {
      pushAssistant("取消失败，任务可能已进入终态。");
    }
  }, [activeRun, pushAssistant]);

  /** 挂载恢复（v4 §11.3）：会话回放 + 待澄清 + 运行中任务续订（after=last_event_id）。 */
  useEffect(() => {
    const pid = initialPlanId ?? "new";
    if (!/^\d+$/.test(pid)) return;
    let alive = true;
    void (async () => {
      try {
        const ws = await getAgentWorkspace(pid);
        if (!alive) return;
        const restored: ChatMessage[] = ws.conversation.map((turn) => ({
          id: crypto.randomUUID(),
          role: turn.role,
          content: turn.content,
          timestamp: "",
          cards: turn.route_plan ? [{ data: { route_plan: turn.route_plan } }] : undefined,
        }));
        setMessages(restored);
        for (const clar of ws.open_clarifications ?? []) {
          setMessages((prev) => [...prev, newMessage("assistant", clarificationText(clar))]);
        }
        if (ws.active_run) {
          subscribeRun(ws.active_run, pid, ws.last_event_id ?? 0);
        }
      } catch {
        /* workspace 不可用（如旧 plan 无 agent 数据）→ 空会话开始，不阻塞输入 */
      }
    })();
    return () => {
      alive = false;
      closeRunSource();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPlanId]);

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || sending) return;
    setMessages((prev) => [...prev, newMessage("user", message)]);
    setInput("");
    setSending(true);
    try {
      const d: AgentTurnResponse = await agentTurn(planId, message);
      const pid = d.plan_id ?? planId;
      if (d.plan_id && planId === "new") {
        setPlanId(d.plan_id);
        // 用原生 history API 更新 URL：router.replace 会改变 searchParams →
        // page.tsx 的 key={plan} 触发 ChatPanel 重挂载、清空当前会话（联调发现的 bug）。
        window.history.replaceState(null, "", `/chat?plan=${d.plan_id}`);
      }
      const reply = d.assistant_message?.content || d.reply || "";
      if (reply) {
        // design_itinerary：路线卡挂到回复气泡（DD-15 v1.1）
        pushAssistant(
          reply,
          d.route_plan ? [{ data: { route_plan: d.route_plan } }] : undefined,
        );
      }
      if (d.clarification) pushAssistant(clarificationText(d.clarification));
      if (d.error?.message) {
        pushAssistant(`⚠ ${d.error.message}${d.error.recovery ?? ""}`);
      }
      if (d.run) subscribeRun(d.run, pid, 0);
    } catch {
      pushAssistant("网络异常，请稍后重试");
    } finally {
      setSending(false);
    }
  };

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, researchPhase]);

  const busy = sending; // run 执行中不锁输入：v4 支持追加/澄清回合并行提交

  return (
    <div className="flex flex-col h-full min-h-0 bg-background">
      {/* 消息流 */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <p className="text-sm text-secondary text-center mt-8">
            说说你的周末想法，比如「上海出发，两三千预算，想看展」
          </p>
        )}
        <AnimatePresence>
          {messages.map((msg) => (
            <motion.div
              key={msg.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2 }}
            >
              <MessageBubble
                message={msg}
                planId={planId}
                onOptionClick={(opt) => void send(opt)}
              />
            </motion.div>
          ))}
        </AnimatePresence>
        {researchPhase && (
          <div className="space-y-1">
            <ResearchProgress phase={researchPhase.phase} message={researchPhase.message} />
            {activeRun && (
              <div className="flex items-center gap-2 px-3 text-xs text-secondary">
                <span className="truncate">任务：{activeRun.goal ?? activeRun.type ?? ""}</span>
                <button
                  onClick={() => void cancelRun()}
                  className="shrink-0 px-2 py-1 border border-border rounded-card text-secondary
                             hover:text-primary hover:border-accent-green/60"
                >
                  取消
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* 快捷指令（横滑，点选填入输入框） */}
      <QuickCommands onSelect={(cmd) => setInput(cmd)} disabled={busy} />

      {/* 输入框 */}
      <div className="p-3 border-t border-border bg-card">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void send(input)}
            placeholder="比如：周六下午上海有什么展览？"
            aria-label="输入消息"
            className="flex-1 min-h-[44px] px-3 py-2 border border-border rounded-card text-sm
                       placeholder:text-secondary/70
                       focus:outline-none focus:ring-2 focus:ring-accent-green/50"
          />
          <Button
            variant="accent"
            onClick={() => void send(input)}
            disabled={busy || !input.trim()}
          >
            发送
          </Button>
        </div>
      </div>
    </div>
  );
}
