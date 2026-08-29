// components/ui/Skeleton.tsx
// 加载占位（animate-pulse）。单行：<Skeleton className="h-4 w-2/3" />；
// 多行文本块：<Skeleton lines={3} />。
export interface SkeletonProps {
  className?: string;
  /** 多行骨架（每行 h-4，末行 2/3 宽） */
  lines?: number;
}

export function Skeleton({ className = "h-4 w-full", lines }: SkeletonProps) {
  if (lines && lines > 1) {
    return (
      <div className="space-y-2" aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <div
            key={i}
            className={`animate-pulse rounded bg-border/60 h-4 ${
              i === lines - 1 ? "w-2/3" : "w-full"
            }`}
          />
        ))}
      </div>
    );
  }
  return (
    <div aria-hidden="true" className={`animate-pulse rounded bg-border/60 ${className}`} />
  );
}
