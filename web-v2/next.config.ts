import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next 16 默认拦截跨源 dev 资源（HMR/客户端 chunk）；允许 127.0.0.1 与 localhost 互访
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Proxy /api/* to the BFF (uvicorn on :8000) so pages avoid CORS in dev
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/:path*",
      },
    ];
  },
};

export default nextConfig;
