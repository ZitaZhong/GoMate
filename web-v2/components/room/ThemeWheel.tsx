// components/room/ThemeWheel.tsx
// 主题转盘（DD-19 §3.4，服务端权威结果 + 前端纯动画）：
// ① 先 POST /rooms/{id}/theme/wheel 取服务端结果（硬约束排除 + 偏好加权 + 次数控制）；
// ② 前端不做任何本地随机，只按结果计算落点播放"转到指定结果"动画（3s，§2.4 显式偏差）；
// ③ spins_left 控制反悔（上限 2 次），409 提示次数用完。
"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { ApiError, spinWheel } from "@/lib/api";
import type { WheelResult } from "@/lib/types";

export interface ThemeWheelProps {
  roomId: string;
  /** 首次转动前用于预渲染扇区的候选主题（来自 summary.theme_candidates） */
  candidates?: string[];
  onResult: (theme: string) => void;
}

export function ThemeWheel({ roomId, candidates, onResult }: ThemeWheelProps) {
  const [spinning, setSpinning] = useState(false);
  const [rotation, setRotation] = useState(0);
  const [result, setResult] = useState<WheelResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const spin = async () => {
    if (spinning) return;
    setSpinning(true);
    setError(null);

    // ① 先向服务端取结果
    let wr: WheelResult;
    try {
      wr = await spinWheel(roomId);
    } catch (e) {
      setSpinning(false);
      setError(
        e instanceof ApiError && e.status === 409
          ? "转盘次数已用完，请直接选择主题"
          : "转盘失败，请重试",
      );
      return;
    }

    // ② 按服务端结果计算落点：等分扇区，指针指向选中项
    const themes = wr.weights.map((w) => w.theme);
    const idx = Math.max(themes.indexOf(wr.theme), 0);
    const seg = 360 / Math.max(themes.length, 1);
    const target = 360 - idx * seg - seg / 2; // 选中扇区中心的绝对角度
    // 至少再转 5 圈，且最终模 360 精确落在 target
    setRotation((prev) => prev + 360 * 5 + (((target - prev) % 360) + 360) % 360);

    // ③ 动画结束后才展示结果
    setTimeout(() => {
      setSpinning(false);
      setResult(wr);
      onResult(wr.theme);
    }, 3000);
  };

  // 结果返回前用候选预渲染扇区；权重只影响服务端抽取，不改变扇区大小
  const themes = result?.weights.map((w) => w.theme) ?? candidates ?? [];

  return (
    <div className="flex flex-col items-center gap-6">
      <div className="relative w-64 h-64">
        <motion.div
          className="w-full h-full rounded-full border-4 border-border overflow-hidden bg-card"
          animate={{ rotate: rotation }}
          transition={{ duration: 3, ease: "easeOut" }}
        >
          {themes.map((t, i) => {
            const angle = (360 / themes.length) * i;
            return (
              <div
                key={t}
                className="absolute inset-0 flex items-center justify-center"
                style={{ transform: `rotate(${angle}deg)` }}
              >
                <span
                  className="absolute text-xs font-medium text-primary"
                  style={{ transform: `translateY(-90px) rotate(${-angle}deg)` }}
                >
                  {t}
                </span>
              </div>
            );
          })}
        </motion.div>
        {/* 指针（globals.css .clip-triangle） */}
        <div
          className="absolute top-0 left-1/2 -translate-x-1/2 -translate-y-2
                     w-4 h-6 bg-accent-coral clip-triangle"
        />
      </div>

      {error && <p className="text-sm text-accent-red">{error}</p>}

      {/* 结果：spins_left 控制反悔（一次反悔 = 最多 2 次） */}
      {result && !spinning && (
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.3 }}
          className="text-center"
        >
          <p className="text-lg font-semibold text-primary">
            本次主题：{result.theme}
          </p>
          {result.excluded.length > 0 && (
            <p className="text-xs text-secondary mt-1">
              已按硬约束排除：{result.excluded.join("、")}
            </p>
          )}
          {result.spins_left > 0 ? (
            <button
              onClick={spin}
              className="mt-2 min-h-[44px] text-sm text-accent-blue underline"
            >
              不满意？再转一次（仅一次机会）
            </button>
          ) : (
            <p className="mt-2 text-xs text-secondary">
              反悔次数已用完，可确认或改选其他主题
            </p>
          )}
        </motion.div>
      )}

      {/* 开始按钮 */}
      {!result && (
        <button
          onClick={spin}
          disabled={spinning}
          className="px-6 py-3 min-h-[44px] bg-accent-yellow text-primary font-medium rounded-card
                     disabled:opacity-50 hover:bg-accent-yellow/80 transition shadow-sm"
        >
          {spinning ? "转动中…" : "开始转动"}
        </button>
      )}
    </div>
  );
}
