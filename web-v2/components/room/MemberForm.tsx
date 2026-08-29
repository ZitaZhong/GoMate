// components/room/MemberForm.tsx
// 成员信息表单（DD-19 §5.5 / PRD §7.2）：
// 出发地三选一（当前位置[Geolocation 需授权] / 搜索地标[防抖，无 key 手输兜底] / 手输区域）、
// 最早/最晚时间、兴趣快捷标签+补充、硬约束单独分组、预算、出行偏好、补充说明。
// 提交：PUT /rooms/{id}/members/{mid}（body 带 member_token，§5.4）。
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, updateMember } from "@/lib/api";
import type { RoomSession } from "@/lib/store";
import {
  HARD_CONSTRAINT_TAGS,
  INTEREST_TAGS,
  TRANSPORT_PREFS,
} from "@/lib/constants";
import type { RoomMember } from "@/lib/types";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Tag } from "@/components/ui/Tag";

type OriginMode = "manual" | "current" | "search";

interface PoiSuggestion {
  name: string;
  address: string;
  lng: number;
  lat: number;
}

/** 高德 Web 服务 key（可选；无 key 时搜索地标降级为手输，PRD §7.2.1 兜底） */
const AMAP_KEY = process.env.NEXT_PUBLIC_AMAP_KEY;

export interface MemberFormProps {
  roomId: string;
  /** 匿名会话（由 useRoomGuard 在客户端异步读取后传入；null 表示本机未加入） */
  session: RoomSession | null;
  /** 城市名（POI 搜索偏置） */
  city?: string;
  /** 编辑场景的回填数据 */
  initial?: RoomMember | null;
  onSubmitted?: () => void;
}

export function MemberForm({ roomId, session, city, initial, onSubmitted }: MemberFormProps) {
  const router = useRouter();

  const [originMode, setOriginMode] = useState<OriginMode>("manual");
  const [originName, setOriginName] = useState(initial?.origin_name ?? "");
  const [coords, setCoords] = useState<{ lng: number; lat: number } | null>(null);
  const [geoState, setGeoState] = useState<"idle" | "locating" | "ok" | "failed">("idle");
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<PoiSuggestion[]>([]);
  const [searching, setSearching] = useState(false);
  const [earliest, setEarliest] = useState(initial?.earliest_depart ?? "10:00");
  const [latest, setLatest] = useState(initial?.latest_end ?? "21:00");
  const [interests, setInterests] = useState<string[]>(initial?.interests ?? []);
  const [extraInterests, setExtraInterests] = useState("");
  const [hardConstraints, setHardConstraints] = useState<string[]>(
    initial?.hard_constraints ?? [],
  );
  const [negativePrefs, setNegativePrefs] = useState(
    (initial?.negative_prefs ?? []).join("，"),
  );
  const [budgetYuan, setBudgetYuan] = useState(
    initial?.budget != null ? String(initial.budget / 100) : "",
  );
  const [transportPref, setTransportPref] = useState<string>(
    initial?.transport_pref ?? "transit",
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const searchSeq = useRef(0);
  // 选中建议后跳过紧随的一次搜索（query 被回填为选中项名称）
  const skipSearch = useRef(false);

  // 出发地：搜索地标（防抖 400ms；无 key 时跳过请求，手输兜底）
  // 所有 setState 均在 setTimeout 回调内（lint：effect 体内不同步 setState）
  useEffect(() => {
    const needSearch = originMode === "search" && !!AMAP_KEY && !!query.trim();
    const timer = setTimeout(
      async () => {
        if (!needSearch) {
          setSuggestions([]);
          return;
        }
        if (skipSearch.current) {
          skipSearch.current = false;
          return;
        }
        const seq = ++searchSeq.current;
        setSearching(true);
        try {
          const params = new URLSearchParams({
            keywords: query.trim(),
            citylimit: "true",
            offset: "8",
            key: AMAP_KEY ?? "",
          });
          if (city) params.set("city", city);
          const resp = await fetch(`https://restapi.amap.com/v3/place/text?${params}`);
          const data = (await resp.json()) as {
            pois?: { name?: string; address?: unknown; location?: string }[];
          };
          if (seq !== searchSeq.current) return;
          setSuggestions(
            (data.pois ?? [])
              .filter((p) => p.name && p.location)
              .map((p) => {
                const [lng, lat] = String(p.location).split(",").map(Number);
                return {
                  name: String(p.name),
                  address: typeof p.address === "string" ? p.address : "",
                  lng,
                  lat,
                };
              })
              .filter((p) => Number.isFinite(p.lng) && Number.isFinite(p.lat)),
          );
        } catch {
          if (seq === searchSeq.current) setSuggestions([]);
        } finally {
          if (seq === searchSeq.current) setSearching(false);
        }
      },
      needSearch ? 400 : 0,
    );
    return () => clearTimeout(timer);
  }, [query, originMode, city]);

  const locate = useCallback(() => {
    if (!("geolocation" in navigator)) {
      setGeoState("failed");
      return;
    }
    setGeoState("locating");
    // 必须用户手势触发授权（PRD §15.4）；只要区域级精度
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setCoords({ lng: pos.coords.longitude, lat: pos.coords.latitude });
        setOriginName((prev) => prev || "当前位置");
        setGeoState("ok");
      },
      () => setGeoState("failed"),
      { timeout: 10000, maximumAge: 300000 },
    );
  }, []);

  const toggle = (list: string[], setList: (v: string[]) => void, tag: string) =>
    setList(list.includes(tag) ? list.filter((t) => t !== tag) : [...list, tag]);

  const submit = async () => {
    if (!session) return;
    if (!originName.trim()) {
      setError("请填写出发地（区域级即可，如地铁站/商圈）");
      return;
    }
    setBusy(true);
    setError("");
    const extra = extraInterests
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const negatives = negativePrefs
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const budget =
      budgetYuan.trim() && Number(budgetYuan) > 0
        ? Math.round(Number(budgetYuan) * 100)
        : null;
    try {
      await updateMember(roomId, session.member_id, {
        member_token: session.member_token,
        origin_name: originName.trim(),
        origin_lng: coords?.lng ?? null,
        origin_lat: coords?.lat ?? null,
        earliest_depart: earliest,
        latest_end: latest,
        budget,
        interests: [...interests, ...extra],
        hard_constraints: hardConstraints,
        negative_prefs: negatives,
        transport_pref: transportPref as "walk" | "transit" | "drive" | "any",
        note: note.trim() || null,
      });
      onSubmitted?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "提交失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  if (!session) {
    return (
      <div className="bg-card border border-border rounded-card p-4 text-sm text-secondary">
        本机还没有该房间的成员身份，请先从邀请链接加入。
        <button
          onClick={() => router.replace(`/room/${roomId}/invite`)}
          className="ml-2 text-accent-blue underline"
        >
          去加入
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* 出发地（三选一，PRD §7.2.1；仅区域级，不要求精确住址） */}
      <section className="bg-card border border-border rounded-card p-4 space-y-3">
        <p className="text-sm font-medium text-primary">
          你从哪里出发？<span className="text-secondary font-normal">（区域级即可）</span>
        </p>
        <div className="flex gap-2">
          {(
            [
              { value: "current", label: "当前位置" },
              { value: "search", label: "搜索地标" },
              { value: "manual", label: "手动输入" },
            ] as const
          ).map((m) => (
            <Tag
              key={m.value}
              color="green"
              selected={originMode === m.value}
              onClick={() => setOriginMode(m.value)}
            >
              {m.label}
            </Tag>
          ))}
        </div>

        {originMode === "current" && (
          <div className="space-y-2">
            <Button variant="secondary" onClick={locate} disabled={geoState === "locating"}>
              {geoState === "locating" ? "定位中…" : "授权获取当前位置"}
            </Button>
            {geoState === "ok" && (
              <p className="text-xs text-accent-green">已定位，可以给它改个好认的名字</p>
            )}
            {geoState === "failed" && (
              <p className="text-xs text-accent-red">
                定位不可用或被拒绝，可改用手动输入
              </p>
            )}
            <Input
              value={originName}
              onChange={(e) => setOriginName(e.target.value)}
              placeholder="给这个位置起个名字，如：我家附近"
              aria-label="出发地名称"
            />
          </div>
        )}

        {originMode === "search" && (
          <div className="space-y-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索地铁站 / 商圈 / 地标"
              aria-label="搜索地标"
            />
            {!AMAP_KEY && (
              <p className="text-xs text-secondary">
                未配置搜索服务，直接把地名写在下面即可
              </p>
            )}
            {searching && <p className="text-xs text-secondary">搜索中…</p>}
            {suggestions.length > 0 && (
              <ul className="border border-border rounded-card divide-y divide-border overflow-hidden">
                {suggestions.map((s) => (
                  <li key={`${s.name}-${s.lng}`}>
                    <button
                      onClick={() => {
                        setOriginName(s.name);
                        setCoords({ lng: s.lng, lat: s.lat });
                        setSuggestions([]);
                        skipSearch.current = true;
                        setQuery(s.name);
                      }}
                      className="w-full text-left px-3 py-2.5 min-h-[44px] hover:bg-background"
                    >
                      <span className="text-sm text-primary">{s.name}</span>
                      {s.address && (
                        <span className="block text-xs text-secondary">{s.address}</span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <Input
              value={originName}
              onChange={(e) => setOriginName(e.target.value)}
              placeholder="出发地（如：徐家汇地铁站）"
              aria-label="出发地名称"
            />
          </div>
        )}

        {originMode === "manual" && (
          <Input
            value={originName}
            onChange={(e) => setOriginName(e.target.value)}
            placeholder="手动输入区域，如：徐家汇 / 五角场"
            aria-label="出发地"
          />
        )}
      </section>

      {/* 空闲时间（PRD §7.2.2） */}
      <section className="bg-card border border-border rounded-card p-4 space-y-2">
        <p className="text-sm font-medium text-primary">空闲时间</p>
        <div className="flex gap-2">
          <label className="flex-1 text-sm text-secondary">
            最早可出发
            <input
              type="time"
              value={earliest}
              onChange={(e) => setEarliest(e.target.value)}
              className="mt-1 w-full min-h-[44px] px-3 py-2 border border-border rounded-card bg-card text-sm text-primary"
            />
          </label>
          <label className="flex-1 text-sm text-secondary">
            最晚结束
            <input
              type="time"
              value={latest}
              onChange={(e) => setLatest(e.target.value)}
              className="mt-1 w-full min-h-[44px] px-3 py-2 border border-border rounded-card bg-card text-sm text-primary"
            />
          </label>
        </div>
      </section>

      {/* 兴趣偏好（快捷标签多选 + 自然语言补充） */}
      <section className="bg-card border border-border rounded-card p-4 space-y-3">
        <p className="text-sm font-medium text-primary">想玩什么？（可多选）</p>
        <div className="flex flex-wrap gap-2">
          {INTEREST_TAGS.map((tag) => (
            <Tag
              key={tag}
              color="green"
              selected={interests.includes(tag)}
              onClick={() => toggle(interests, setInterests, tag)}
            >
              {tag}
            </Tag>
          ))}
        </div>
        <Input
          value={extraInterests}
          onChange={(e) => setExtraInterests(e.target.value)}
          placeholder="补充兴趣，逗号分隔（可选）"
          aria-label="补充兴趣"
        />
        <Input
          value={negativePrefs}
          onChange={(e) => setNegativePrefs(e.target.value)}
          placeholder="不接受的类型，逗号分隔（可选，如：剧本杀）"
          aria-label="不接受的类型"
        />
      </section>

      {/* 硬约束（PRD §7.2.3，与兴趣分开展示） */}
      <section className="bg-card border border-border rounded-card p-4 space-y-3">
        <p className="text-sm font-medium text-primary">
          硬约束<span className="text-secondary font-normal">（会直接影响推荐，单独分组）</span>
        </p>
        <div className="flex flex-wrap gap-2">
          {HARD_CONSTRAINT_TAGS.map((tag) => (
            <Tag
              key={tag}
              color="coral"
              selected={hardConstraints.includes(tag)}
              onClick={() => toggle(hardConstraints, setHardConstraints, tag)}
            >
              {tag}
            </Tag>
          ))}
        </div>
      </section>

      {/* 预算 + 出行方式 + 补充说明 */}
      <section className="bg-card border border-border rounded-card p-4 space-y-3">
        <Input
          label="人均预算上限（元，可选）"
          type="number"
          min={0}
          value={budgetYuan}
          onChange={(e) => setBudgetYuan(e.target.value)}
          placeholder="如：200"
        />
        <div>
          <p className="text-sm text-secondary mb-2">出行方式偏好</p>
          <div className="flex flex-wrap gap-2">
            {TRANSPORT_PREFS.map((p) => (
              <Tag
                key={p.value}
                color="blue"
                selected={transportPref === p.value}
                onClick={() => setTransportPref(p.value)}
              >
                {p.label}
              </Tag>
            ))}
          </div>
        </div>
        <label className="block text-sm text-secondary">
          补充说明（可选）
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            maxLength={500}
            placeholder="还有什么想让大家知道的？"
            className="mt-1 w-full px-3 py-2 border border-border rounded-card bg-card text-sm text-primary resize-none"
          />
        </label>
      </section>

      {error && <p className="text-sm text-accent-red">{error}</p>}
      <Button variant="accent" fullWidth onClick={submit} disabled={busy}>
        {busy ? "提交中…" : initial?.submitted ? "保存修改" : "提交我的信息"}
      </Button>
    </div>
  );
}
