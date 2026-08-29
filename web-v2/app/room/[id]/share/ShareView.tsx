// app/room/[id]/share/ShareView.tsx
// 分享卡视图（客户端）：getRoomShare 渲染（服务端已脱敏：无精确地址/经纬度/
// 个人预算——前端也只渲染标题与时间，不补充任何敏感信息）。
// 纸张展开动效 400ms（DD-19 §2.4）。
"use client";

import { useCallback, useState } from "react";
import { useParams } from "next/navigation";
import { motion } from "framer-motion";
import { getRoomShare } from "@/lib/api";
import type { SharePayload } from "@/lib/types";
import { usePolling } from "@/components/room/usePolling";
import { useRoomGuard } from "@/components/room/useRoomGuard";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

interface ShareNode {
  type?: string;
  title?: string;
  start?: string | null;
}

const hhmm = (iso: string | null | undefined): string => {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
};

export function ShareView() {
  const { id } = useParams<{ id: string }>();
  const { loading, error } = useRoomGuard(id, ["PLANNING", "PUBLISHED"]);
  const [share, setShare] = useState<SharePayload | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setShare(await getRoomShare(id));
    } catch {
      // 轮询重试
    }
  }, [id]);

  // 分享数据轮询（服务端已脱敏）
  usePolling(load, 5000);

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
    } catch {
      window.prompt("复制以下内容：", window.location.href);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (loading || !share) {
    return (
      <main className="min-h-screen bg-background p-6 max-w-md mx-auto space-y-3">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton lines={4} />
        {error && <p className="text-sm text-accent-red">{error}</p>}
      </main>
    );
  }

  const nodes = ((share.itinerary as { nodes?: ShareNode[] } | null)?.nodes ??
    []) as ShareNode[];

  return (
    <main className="min-h-screen bg-background flex flex-col items-center justify-center p-6">
      {/* 纸张展开动效（400ms，DD-19 §2.4） */}
      <motion.div
        initial={{ opacity: 0, scaleY: 0.6, transformOrigin: "top center" }}
        animate={{ opacity: 1, scaleY: 1 }}
        transition={{ duration: 0.4, ease: "easeOut" }}
        className="w-full max-w-sm bg-card border border-border rounded-card shadow-md p-6 space-y-4"
      >
        <div className="text-center space-y-1">
          <p className="font-handwrite text-2xl text-accent-green">GoMate</p>
          <h1 className="text-xl font-semibold text-primary">
            {share.theme ?? "周末计划"}
          </h1>
          <p className="text-sm text-secondary">
            {share.city} · {share.activity_date}
          </p>
          <p className="text-xs text-secondary">
            {share.members.map((m) => m.nickname).join("、")}
          </p>
        </div>

        {nodes.length > 0 && (
          <ol className="space-y-2 border-t border-border pt-3">
            {nodes.map((n, i) => (
              <li key={i} className="flex items-baseline gap-2 text-sm">
                <span className="text-secondary w-12 shrink-0 text-xs">
                  {hhmm(n.start)}
                </span>
                <span className="text-primary">{n.title}</span>
              </li>
            ))}
          </ol>
        )}

        <p className="text-xs text-secondary text-center">
          这个周末，别再问“去哪儿都行”。
        </p>
      </motion.div>

      <div className="w-full max-w-sm mt-4">
        <Button variant="accent" fullWidth onClick={copyLink}>
          {copied ? "已复制链接" : "复制分享链接"}
        </Button>
      </div>
    </main>
  );
}
