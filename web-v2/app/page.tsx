"use client";

import { useState } from "react";
import Link from "next/link";
import { HandDrawnHero } from "@/components/HandDrawnHero";

export default function HomePage() {
  const [inviteCode, setInviteCode] = useState("");

  return (
    <main className="min-h-screen bg-background flex flex-col items-center justify-center p-6">
      {/* 手绘装饰 + Logo & 主标题（PRD §12.2 指定文案，不得擅改） */}
      <div className="text-center mb-10 flex flex-col items-center">
        <HandDrawnHero className="w-40 h-28 text-accent-green" />
        <h1 className="mt-2">
          <span className="block font-handwrite text-4xl text-accent-green">GoMate</span>
          <span className="block text-base text-secondary mt-2">
            这个周末，别再问&ldquo;去哪儿都行&rdquo;。
          </span>
        </h1>
      </div>

      {/* Dual-mode Entry */}
      <div className="w-full max-w-sm space-y-3">
        {/* 市内活动模式 - Room（PRD 主按钮） */}
        <Link
          href="/room/create"
          className="block w-full p-5 bg-card border border-border rounded-card
                     hover:border-accent-green transition-colors text-center"
        >
          <p className="font-medium text-primary">发起一个周末计划</p>
          <p className="text-sm text-secondary mt-1">
            邀请朋友，一起决定做什么
          </p>
        </Link>

        {/* 对话式规划（双模式显式扩展入口，DD-19 §5.1） */}
        <Link
          href="/chat"
          className="block w-full p-5 bg-card border border-border rounded-card
                     hover:border-accent-blue transition-colors text-center"
        >
          <p className="font-medium text-primary">和 AI 聊聊周末安排</p>
          <p className="text-sm text-secondary mt-1">
            跨城出行或市内活动都行
          </p>
        </Link>

        {/* 加入房间（PRD 主按钮） */}
        <div className="flex gap-2 pt-2">
          <input
            value={inviteCode}
            onChange={(e) => setInviteCode(e.target.value)}
            placeholder="输入邀请码加入"
            className="flex-1 min-h-[44px] px-3 py-2.5 border border-border rounded-card
                       text-sm bg-card focus:outline-none focus:ring-2 focus:ring-accent-green/40"
          />
          <button
            onClick={() => {
              if (inviteCode.trim()) {
                window.location.href = `/room/join?code=${inviteCode.trim()}`;
              }
            }}
            className="min-h-[44px] px-5 py-2.5 bg-primary text-white rounded-card
                       text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            加入
          </button>
        </div>
      </div>

      {/* Footer hint */}
      <p className="text-xs text-secondary mt-12 text-center max-w-xs">
        证据优先 · 不做交易 · 当周即研
      </p>
    </main>
  );
}
