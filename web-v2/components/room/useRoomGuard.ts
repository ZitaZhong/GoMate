// components/room/useRoomGuard.ts
// 房间路由守卫（DD-19 §5.3）：进入任意 /room/[id]/* 页面先 GET /rooms/{id}，
// 当前路由与状态不符时重定向到状态对应页；EXPIRED 一律回 /room/[id]（只读页）。
// 同时承担非流式页的 5s 轮询（§4.3）：状态被其他成员推进后自动跳转。
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ApiError, getRoom } from "@/lib/api";
import { getRoomSession, type RoomSession } from "@/lib/store";
import type { Room, RoomMember, RoomStatus } from "@/lib/types";
import { usePolling } from "./usePolling";

/**
 * 状态 → 规范路由（DD-19 §5.3）。
 * COLLECTING 按当前用户（localStorage token）是否已提交区分 member/summary；
 * ACTIVITY_SELECTED 的 aid 不在 getRoom 响应里，用占位段 "selected"
 * （活动详情数据实际来自 getRoomRoutes/getRoomPlan，aid 仅作展示）。
 */
export function canonicalRoomPath(
  roomId: string | number,
  status: RoomStatus,
  session: RoomSession | null,
  members: RoomMember[],
): string {
  const base = `/room/${roomId}`;
  switch (status) {
    case "DRAFT":
      return `${base}/invite`;
    case "COLLECTING": {
      const me = session
        ? members.find((m) => m.member_id === session.member_id)
        : undefined;
      return me?.submitted ? `${base}/summary` : `${base}/member`;
    }
    case "THEME_SELECTING":
      return `${base}/theme`;
    case "RECOMMENDING":
      return `${base}/recommend`;
    case "ACTIVITY_SELECTED":
      return `${base}/activity/selected`;
    case "PLANNING":
    case "PUBLISHED":
      return `${base}/plan`;
    case "EXPIRED":
      return base;
  }
}

export interface RoomGuardResult {
  room: Room | null;
  members: RoomMember[];
  session: RoomSession | null;
  /** 初始加载中（尚无 room 数据） */
  loading: boolean;
  error: string | null;
  /** 手动重拉（提交操作后调用，避免等下一轮轮询） */
  reload: () => Promise<void>;
}

/**
 * @param roomId 路由段 id
 * @param allowed 本页面服务的状态集合；room.status 不在其中时按 §5.3 重定向
 * @param pollMs 轮询间隔（默认 5000；传 0 关闭轮询，如流式页面）
 */
export function useRoomGuard(
  roomId: string,
  allowed: RoomStatus[],
  pollMs = 5000,
): RoomGuardResult {
  const router = useRouter();
  const pathname = usePathname();
  const [room, setRoom] = useState<Room | null>(null);
  const [members, setMembers] = useState<RoomMember[]>([]);
  const [error, setError] = useState<string | null>(null);
  // session 在异步 reload 内读取（避免 SSR 直读 localStorage 造成 hydration 不一致）
  const [session, setSession] = useState<RoomSession | null>(null);
  // allowed 由调用方字面量传入（每次渲染新数组），用 ref 保持最新值（effect 内同步）
  const allowedRef = useRef(allowed);
  useEffect(() => {
    allowedRef.current = allowed;
  }, [allowed]);

  const reload = useCallback(async () => {
    setSession(getRoomSession(roomId));
    try {
      const data = await getRoom(roomId);
      setRoom(data.room);
      setMembers(data.members);
      setError(null);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 404
          ? "房间不存在或链接有误"
          : "加载房间失败，请稍后重试",
      );
    }
  }, [roomId]);

  // 初始加载 + 非流式页轮询（流式页 pollMs=0 仅一次性加载，避免与 SSE 重复）
  usePolling(reload, pollMs);

  // 状态不符 → 重定向到规范路由（EXPIRED → /room/[id] 只读页）
  useEffect(() => {
    if (!room) return;
    if (allowedRef.current.includes(room.status)) return;
    const target = canonicalRoomPath(roomId, room.status, session, members);
    if (target !== pathname) router.replace(target);
  }, [room, members, session, roomId, pathname, router]);

  return {
    room,
    members,
    session,
    loading: !room && !error,
    error,
    reload,
  };
}
