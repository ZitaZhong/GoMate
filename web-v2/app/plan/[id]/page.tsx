// app/plan/[id]/page.tsx
// Trip Bundle 视图页（DD-19 §5.2）：
// 桌面（≥1024px）左右分栏 Bundle | ChatPanel；移动端 Bundle 全屏 + 底部「继续聊」抽屉。
import type { Metadata } from "next";
import { PlanView } from "./plan-view";

export const metadata: Metadata = {
  title: "周末计划 · GoMate",
  description: "你的跨城周末 Trip Bundle：城市、交通、计划与预算",
};

export default async function PlanPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <PlanView planId={id} />;
}
