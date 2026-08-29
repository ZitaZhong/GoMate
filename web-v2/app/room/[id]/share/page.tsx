// app/room/[id]/share/page.tsx
// 分享页（服务端组件）：generateMetadata 输出 OG meta（微信内分享，DD-19 §7）；
// 交互渲染交给客户端 ShareView。分享数据由 GET /rooms/{id}/share 提供
// （服务端已脱敏：无精确地址/经纬度/个人预算）。
import type { Metadata } from "next";
import { ShareView } from "./ShareView";

interface PageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { id } = await params;
  const fallback: Metadata = {
    title: "GoMate 周末计划分享",
    description: "这个周末，别再问“去哪儿都行”。",
    openGraph: {
      title: "GoMate 周末计划分享",
      description: "这个周末，别再问“去哪儿都行”。",
    },
  };
  try {
    // 与 next.config.ts rewrites 同源（BFF :8000）；渲染期请求，不缓存
    const resp = await fetch(`http://127.0.0.1:8000/rooms/${id}/share`, {
      cache: "no-store",
    });
    if (!resp.ok) return fallback;
    const share = (await resp.json()) as {
      city?: string;
      activity_date?: string;
      theme?: string | null;
      members?: { nickname: string }[];
    };
    const title = `${share.city ?? ""} ${share.activity_date ?? ""} · ${
      share.theme ?? "周末计划"
    }`;
    const description = `${(share.members ?? []).map((m) => m.nickname).join("、")} 的周末计划，一起来看看吧。`;
    return {
      title,
      description,
      openGraph: { title, description },
    };
  } catch {
    return fallback;
  }
}

export default function RoomSharePage() {
  return <ShareView />;
}
