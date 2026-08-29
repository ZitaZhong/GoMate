// components/HandDrawnHero.tsx
// 首页手绘风装饰（PRD §12.2 / DD-19 §5.1）：内联 SVG，
// 手绘地图路线 + 转盘意象，accent 色细线条，不用 emoji。
export function HandDrawnHero({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 160 112"
      fill="none"
      className={className}
      aria-hidden="true"
      role="presentation"
    >
      {/* 手绘地图路线（抖动虚线） */}
      <path
        d="M14 88 C 34 60, 44 96, 66 72 S 96 40, 118 54"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeDasharray="5 5"
      />
      {/* 起点 */}
      <circle cx="14" cy="88" r="4" stroke="currentColor" strokeWidth="1.5" />
      {/* 途经点 */}
      <circle cx="66" cy="72" r="2.5" fill="currentColor" />
      {/* 终点旗帜 */}
      <path
        d="M118 54 L118 36 M118 36 L132 41 L118 47"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* 转盘意象（右上角小轮盘） */}
      <circle cx="128" cy="84" r="18" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M128 66 L128 102 M110 84 L146 84 M116 72 L140 96 M140 72 L116 96"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinecap="round"
      />
      {/* 转盘指针 */}
      <path
        d="M128 62 L124 70 L132 70 Z"
        fill="currentColor"
      />
      {/* 小太阳/星星点缀 */}
      <path
        d="M40 24 L40 30 M34 27 L46 27 M36 21 L44 33 M44 21 L36 33"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
    </svg>
  );
}
