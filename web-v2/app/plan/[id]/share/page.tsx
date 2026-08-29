// app/plan/[id]/share/page.tsx
// 跨城分享页（DD-13 §7.3 脱敏）：不渲染精确地址/经纬度/个人预算/费用明细。
// OG meta 由 generateMetadata 输出；数据在客户端经 /api 代理拉取后白名单渲染。
import type { Metadata } from "next";
import { ShareView } from "./share-view";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const title = "这个周末，去另一座城市 · GoMate";
  const description = "朋友分享的周末跨城计划：目的地、主题与行程亮点（已脱敏）。";
  return {
    title,
    description,
    openGraph: {
      title,
      description,
      type: "website",
      url: `/plan/${id}/share`,
      siteName: "GoMate 周末去哪儿",
    },
  };
}

export default async function PlanSharePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ShareView planId={id} />;
}
