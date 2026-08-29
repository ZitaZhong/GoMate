// components/room/ThemeVote.tsx
// 主题投票（DD-18）：每个候选主题可按 VOTE_WEIGHTS 投 强烈喜欢(3)/可接受(1)/不喜欢(-2)，
// POST /rooms/{id}/theme/vote（body 带 member_token），响应 tally 按总分降序展示。
"use client";

import { useState } from "react";
import { ApiError, voteTheme } from "@/lib/api";
import { VOTE_WEIGHTS } from "@/lib/constants";
import type { VoteResponse } from "@/lib/types";

export interface ThemeVoteProps {
  roomId: string;
  /** 候选主题（summary.theme_candidates） */
  candidates: string[];
  memberToken: string | null;
  /** 投票后回调（父组件可刷新 tally 展示或给出确认入口） */
  onTally?: (tally: VoteResponse["tally"]) => void;
}

export function ThemeVote({ roomId, candidates, memberToken, onTally }: ThemeVoteProps) {
  // 本地记住每个主题最近一次投的权重（回显选中态）
  const [myVotes, setMyVotes] = useState<Record<string, number>>({});
  const [tally, setTally] = useState<VoteResponse["tally"]>([]);
  const [error, setError] = useState("");
  const [busyTheme, setBusyTheme] = useState<string | null>(null);

  const vote = async (theme: string, weight: 1 | 3 | -2) => {
    if (!memberToken) {
      setError("请先加入房间再投票");
      return;
    }
    setBusyTheme(theme);
    setError("");
    try {
      const resp = await voteTheme(roomId, { member_token: memberToken, theme, weight });
      setMyVotes((prev) => ({ ...prev, [theme]: weight }));
      setTally(resp.tally);
      onTally?.(resp.tally);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "投票失败，请重试");
    } finally {
      setBusyTheme(null);
    }
  };

  const scoreOf = (theme: string) => tally.find((t) => t.theme === theme)?.score;

  return (
    <div className="space-y-3">
      {!memberToken && (
        <p className="text-xs text-accent-coral">本机还没有成员身份，无法投票</p>
      )}
      <ul className="space-y-2">
        {candidates.map((theme) => {
          const score = scoreOf(theme);
          return (
            <li
              key={theme}
              className="bg-card border border-border rounded-card p-3 space-y-2"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-primary">{theme}</span>
                {score !== undefined && (
                  <span className="text-xs text-secondary">总分 {score}</span>
                )}
              </div>
              <div className="flex gap-2">
                {VOTE_WEIGHTS.map((w) => {
                  const active = myVotes[theme] === w.weight;
                  return (
                    <button
                      key={w.weight}
                      onClick={() => vote(theme, w.weight)}
                      disabled={busyTheme === theme}
                      aria-pressed={active}
                      className={`flex-1 min-h-[44px] px-2 rounded-lg border text-xs transition
                        disabled:opacity-50
                        ${
                          active
                            ? w.weight === -2
                              ? "bg-accent-red text-white border-transparent"
                              : "bg-accent-green text-white border-transparent"
                            : "border-border text-secondary hover:bg-background"
                        }`}
                    >
                      {w.label}
                    </button>
                  );
                })}
              </div>
            </li>
          );
        })}
      </ul>
      {tally.length > 0 && (
        <p className="text-xs text-secondary">
          当前领先：{tally[0].theme}（{tally[0].score} 分）
        </p>
      )}
      {error && <p className="text-sm text-accent-red">{error}</p>}
    </div>
  );
}
