// app/chat/page.tsx
// 对话页（DD-19 §5.2：跨城模式主入口，规划驱动页）。
// 移动端 ChatPanel 全屏；桌面（≥1024px）居中卡片。支持 ?plan={id} 恢复会话。
import type { Metadata } from "next";
import Link from "next/link";
import { ChatPanel } from "@/components/chat/ChatPanel";

export const metadata: Metadata = {
  title: "和 AI 聊聊周末安排 · GoMate",
  description: "对话式周末规划：跨城出行或市内活动都行",
};

export default async function ChatPage({
  searchParams,
}: {
  searchParams: Promise<{ plan?: string }>;
}) {
  const { plan } = await searchParams;

  return (
    <main className="h-dvh flex flex-col bg-background lg:items-center lg:justify-center lg:py-8">
      <div className="flex-1 min-h-0 w-full flex flex-col lg:max-w-2xl lg:h-[85vh] lg:flex-none
                      lg:bg-card lg:border lg:border-border lg:rounded-card lg:overflow-hidden">
        <header className="flex items-center justify-between px-4 py-2 border-b border-border bg-card">
          <Link href="/" className="font-handwrite text-xl text-accent-green">
            GoMate
          </Link>
          <span className="text-xs text-secondary">对话式周末规划</span>
        </header>
        <div className="flex-1 min-h-0">
          {/* key 保证切换 plan 时重置会话状态 */}
          <ChatPanel key={plan ?? "new"} planId={plan} />
        </div>
      </div>
    </main>
  );
}
