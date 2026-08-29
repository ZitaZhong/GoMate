// lib/store.ts
// 房间匿名会话（DD-19 §5.4）：未登录可用，member_token 仅放 POST/PUT body。
// 持久化：localStorage["gomate:room:{roomId}"] = { member_id, member_token, nickname }。
// token 丢失（换设备/清缓存）→ 凭邀请链接重新加入，MVP 接受重复成员。

import { create } from "zustand";

export interface RoomSession {
  member_id: number;
  member_token: string;
  nickname: string;
}

const storageKey = (roomId: string | number) => `gomate:room:${roomId}`;

function readStored(roomId: string | number): RoomSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey(roomId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<RoomSession>;
    if (typeof parsed.member_id !== "number" || typeof parsed.member_token !== "string") {
      return null;
    }
    return {
      member_id: parsed.member_id,
      member_token: parsed.member_token,
      nickname: typeof parsed.nickname === "string" ? parsed.nickname : "",
    };
  } catch {
    return null;
  }
}

interface RoomSessionState {
  /** 内存缓存（roomId → session）；读取时未命中回退 localStorage。 */
  sessions: Record<string, RoomSession>;
  getSession: (roomId: string | number) => RoomSession | null;
  setSession: (roomId: string | number, session: RoomSession) => void;
  clearSession: (roomId: string | number) => void;
}

export const useRoomSessionStore = create<RoomSessionState>((set, get) => ({
  sessions: {},

  getSession: (roomId) => {
    const key = String(roomId);
    const cached = get().sessions[key];
    if (cached) return cached;
    const stored = readStored(key);
    if (stored) {
      set((state) => ({ sessions: { ...state.sessions, [key]: stored } }));
    }
    return stored;
  },

  setSession: (roomId, session) => {
    const key = String(roomId);
    if (typeof window !== "undefined") {
      try {
        window.localStorage.setItem(storageKey(key), JSON.stringify(session));
      } catch {
        // localStorage 不可用/超限：仅保留内存态
      }
    }
    set((state) => ({ sessions: { ...state.sessions, [key]: session } }));
  },

  clearSession: (roomId) => {
    const key = String(roomId);
    if (typeof window !== "undefined") {
      try {
        window.localStorage.removeItem(storageKey(key));
      } catch {
        // noop
      }
    }
    set((state) => {
      const next = { ...state.sessions };
      delete next[key];
      return { sessions: next };
    });
  },
}));

// ---- 非组件环境（事件处理器/工具函数）可用的命令式封装 ----

export function getRoomSession(roomId: string | number): RoomSession | null {
  return useRoomSessionStore.getState().getSession(roomId);
}

export function saveRoomSession(roomId: string | number, session: RoomSession): void {
  useRoomSessionStore.getState().setSession(roomId, session);
}

export function clearRoomSession(roomId: string | number): void {
  useRoomSessionStore.getState().clearSession(roomId);
}
