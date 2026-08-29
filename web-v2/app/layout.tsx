import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Self-hosted handwriting fonts (DD-19 §2.3): no Google Fonts at build time.
// Latin glyphs come from Caveat, CJK glyphs from ZCOOL KuaiLe; the composed
// `--font-handwrite-loaded` var is consumed by `--font-handwrite` in globals.css.
const caveat = localFont({
  src: [
    { path: "../public/fonts/caveat-latin-400.woff2", weight: "400", style: "normal" },
    { path: "../public/fonts/caveat-latin-700.woff2", weight: "700", style: "normal" },
  ],
  variable: "--font-caveat-loaded",
  display: "swap",
  fallback: ["Caveat", "cursive"],
});

const zcoolKuaiLe = localFont({
  src: [{ path: "../public/fonts/zcool-kuaile-cn-400.woff2", weight: "400", style: "normal" }],
  variable: "--font-zcool-loaded",
  display: "swap",
  fallback: ["ZCOOL KuaiLe", "cursive"],
});

export const metadata: Metadata = {
  title: "GoMate - 周末计划",
  description: "这个周末，别再问“去哪儿都行”。跨城出行或市内活动，AI 帮你规划。",
  manifest: "/manifest.json",
};

export const viewport: Viewport = {
  themeColor: "#8FB59B",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      className={`${caveat.variable} ${zcoolKuaiLe.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
