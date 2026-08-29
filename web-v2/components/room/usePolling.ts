// components/room/usePolling.ts
// 非流式页面多人同步（DD-19 §4.3）：轮询 + 窗口聚焦即刷新。
// 说明：根 layout 无 TanStack Query Provider（app/layout.tsx 不可改），
// 用 setInterval + focus 监听实现同等语义（refetchInterval + refetchOnWindowFocus）。
"use client";

import { useEffect } from "react";

/**
 * 挂载后立即执行一次 fn（immediate=true 时），之后每 intervalMs 执行一次；
 * 窗口重新聚焦时也执行。intervalMs <= 0 表示只做一次性加载（不轮询）。
 * fn 需用 useCallback 保持稳定；setState 发生在 async 回调内（lint 合规）。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  immediate = true,
) {
  useEffect(() => {
    if (immediate) void fn();
    if (intervalMs <= 0) return;
    const timer = setInterval(() => void fn(), intervalMs);
    const onFocus = () => void fn();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [fn, intervalMs, immediate]);
}
