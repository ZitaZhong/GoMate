// lib/api.ts
// BFF typed client。base 用相对路径 /api（next.config.ts rewrites → http://127.0.0.1:8000）。
// 请求体字段名与后端 Pydantic Body 模型一致（src/wheretogo/bff/rooms.py、app.py）。
// SSE 端点（/stream /research-more /rooms/recommend /rooms/plan/modify）不走 json client，
// 这里只导出 URL helper，消费侧统一用 lib/sse.ts 的 consumeSSE。

import type {
  ActivityCandidate,
  AgentTurnResponse,
  AgentWorkspace,
  ChatDecision,
  ConfirmThemeResponse,
  CreateRoomResponse,
  JoinRoomResponse,
  RoomByInviteResponse,
  RoomDetailResponse,
  RoomPlanResponse,
  RoomRoutesResponse,
  RoomSummary,
  SelectActivityResponse,
  SharePayload,
  VoteResponse,
  WheelResult,
} from "./types";

// API base：默认走 Next rewrites 代理（/api/* → BFF，同源、生产部署用）；
// 本地开发设 NEXT_PUBLIC_API_BASE=http://localhost:8000 直连 BFF
// （Next dev 代理对慢请求会 ECONNRESET、对 SSE 会缓冲，见 DD-19 联调记录）。
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

// ============================ 错误类型 ============================

export class ApiError extends Error {
  readonly status: number;
  /** 后端原始错误体（FastAPI 一般为 {detail: string}） */
  readonly detail?: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// ============================ 请求基础设施 ============================

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, options);
  } catch (e) {
    throw new ApiError(e instanceof Error ? e.message : "网络异常", 0);
  }
  if (!resp.ok) {
    let detail: unknown;
    let message = `请求失败 (${resp.status})`;
    try {
      detail = await resp.json();
      const d = (detail as { detail?: unknown } | null)?.detail;
      if (typeof d === "string") message = d;
    } catch {
      // 非 JSON 错误体，保留默认 message
    }
    throw new ApiError(message, resp.status, detail);
  }
  return (await resp.json()) as T;
}

function jsonOptions(method: "POST" | "PUT", body?: unknown): RequestInit {
  return {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  };
}

// ============================ 请求体类型（对齐后端 Body 模型）============================

/** CreateRoomBody（rooms.py）：activity_date 为 "YYYY-MM-DD"。 */
export interface CreateRoomBody {
  activity_date: string;
  city?: string;
  time_window?: { earliest?: string; latest?: string } | null;
  budget_range?: { min?: number; max?: number; currency?: string } | null;
  creator_nickname?: string;
}

/** JoinRoomBody（rooms.py）。 */
export interface JoinRoomBody {
  nickname: string;
}

/** UpdateMemberBody（rooms.py）：budget 单位为分；时间为 "HH:MM"。 */
export interface UpdateMemberBody {
  member_token: string;
  origin_name?: string | null;
  origin_lng?: number | null;
  origin_lat?: number | null;
  origin_poi_id?: string | null;
  earliest_depart?: string | null;
  latest_end?: string | null;
  budget?: number | null;
  interests?: string[] | null;
  hard_constraints?: string[] | null;
  negative_prefs?: string[] | null;
  transport_pref?: "walk" | "transit" | "drive" | "any" | null;
  note?: string | null;
}

/** VoteBody（rooms.py）：weight 仅支持 1(可接受)/3(强烈喜欢)/-2(不喜欢)。 */
export interface VoteBody {
  member_token: string;
  theme: string;
  weight?: 1 | 3 | -2;
}

/** ConfirmThemeBody（rooms.py）。 */
export interface ConfirmThemeBody {
  theme: string;
  method?: "direct" | "vote" | "ai" | "wheel";
}

/** SelectActivityBody（rooms.py）：传整个 activity 对象或仅 activity_id。 */
export interface SelectActivityBody {
  activity_id?: number | string | null;
  activity?: Partial<ActivityCandidate> | null;
}

/** ModifyBody（rooms.py）：confirm 为 needs_confirmation 后的二次确认。SSE 端点。 */
export interface RoomModifyBody {
  message: string;
  confirm?: boolean;
}

/** ChatBody（app.py）。 */
export interface ChatBody {
  message: string;
  memory_ctx?: Record<string, unknown>;
}

// ============================ 房间端点 ============================

export function createRoom(body: CreateRoomBody): Promise<CreateRoomResponse> {
  return request<CreateRoomResponse>("/rooms", jsonOptions("POST", body));
}

export function getRoomByInvite(code: string): Promise<RoomByInviteResponse> {
  return request<RoomByInviteResponse>(`/rooms/by-invite/${encodeURIComponent(code)}`);
}

export function getRoom(roomId: number | string): Promise<RoomDetailResponse> {
  return request<RoomDetailResponse>(`/rooms/${roomId}`);
}

export function joinRoom(
  roomId: number | string,
  body: JoinRoomBody,
): Promise<JoinRoomResponse> {
  return request<JoinRoomResponse>(`/rooms/${roomId}/members`, jsonOptions("POST", body));
}

export function updateMember(
  roomId: number | string,
  memberId: number | string,
  body: UpdateMemberBody,
): Promise<{ ok: boolean; member_id: number }> {
  return request<{ ok: boolean; member_id: number }>(
    `/rooms/${roomId}/members/${memberId}`,
    jsonOptions("PUT", body),
  );
}

export function getRoomSummary(roomId: number | string): Promise<RoomSummary> {
  return request<RoomSummary>(`/rooms/${roomId}/summary`);
}

export function voteTheme(roomId: number | string, body: VoteBody): Promise<VoteResponse> {
  return request<VoteResponse>(`/rooms/${roomId}/theme/vote`, jsonOptions("POST", body));
}

/** 转盘：服务端权威结果（DD-19 §3.4），前端不做本地随机。次数超限抛 ApiError(409)。 */
export function spinWheel(roomId: number | string): Promise<WheelResult> {
  return request<WheelResult>(`/rooms/${roomId}/theme/wheel`, jsonOptions("POST"));
}

export function confirmTheme(
  roomId: number | string,
  body: ConfirmThemeBody,
): Promise<ConfirmThemeResponse> {
  return request<ConfirmThemeResponse>(
    `/rooms/${roomId}/theme/confirm`,
    jsonOptions("POST", body),
  );
}

/** 回退到主题选择（推荐页空结果时允许换主题）。 */
export function backToTheme(roomId: number | string): Promise<{ ok: boolean; status: string }> {
  return request<{ ok: boolean; status: string }>(
    `/rooms/${roomId}/back-to-theme`,
    jsonOptions("POST"),
  );
}

export function selectActivity(
  roomId: number | string,
  body: SelectActivityBody,
): Promise<SelectActivityResponse> {
  return request<SelectActivityResponse>(
    `/rooms/${roomId}/select-activity`,
    jsonOptions("POST", body),
  );
}

export function getRoomRoutes(roomId: number | string): Promise<RoomRoutesResponse> {
  return request<RoomRoutesResponse>(`/rooms/${roomId}/routes`);
}

export function getRoomPlan(roomId: number | string): Promise<RoomPlanResponse> {
  return request<RoomPlanResponse>(`/rooms/${roomId}/plan`);
}

export function undoRoomPlan(roomId: number | string): Promise<RoomPlanResponse> {
  return request<RoomPlanResponse>(`/rooms/${roomId}/plan/undo`, jsonOptions("POST"));
}

/** 分享卡（服务端已脱敏：无精确地址/坐标/个人预算）。 */
export function getRoomShare(roomId: number | string): Promise<SharePayload> {
  return request<SharePayload>(`/rooms/${roomId}/share`);
}

// ============================ 对话 Copilot ============================

/** 旧版一轮对话（legacy /plans/{id}/chat）；对话面板已全量迁移 v4 agentTurn，仅留作兼容。 */
export function chatTurn(planId: string, message: string): Promise<ChatDecision> {
  const body: ChatBody = { message };
  return request<ChatDecision>(
    `/plans/${encodeURIComponent(planId)}/chat`,
    jsonOptions("POST", body),
  );
}

// ============================ v4 Agent（/agent/*，v4 回合状态机）============================

/** 一轮对话 = 一次持久化事务（POST turns，带幂等键；202=任务运行中）。 */
export function agentTurn(planId: string, message: string): Promise<AgentTurnResponse> {
  const key =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return request<AgentTurnResponse>(
    `/agent/conversations/${encodeURIComponent(planId)}/turns`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify({ message, idempotency_key: key }),
    },
  );
}

/** 完整工作区快照（刷新/断线重连后恢复会话、运行中任务、待澄清、当前方案）。 */
export function getAgentWorkspace(planId: string): Promise<AgentWorkspace> {
  return request<AgentWorkspace>(
    `/agent/conversations/${encodeURIComponent(planId)}/workspace`,
  );
}

/** 取消运行中任务（幂等；终态 run 重复取消返回当前状态）。 */
export function cancelAgentRun(runId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/agent/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  );
}

/** 回合可审计链路（turn → interpretation → run → events）。 */
export function getTurnTrace(turnId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(
    `/agent/turns/${encodeURIComponent(turnId)}/trace`,
  );
}

/** Run 事件流 URL（EventSource 用；自动重连携 Last-Event-ID 续传）。 */
export function agentRunEventsUrl(run: { id: string; events_url?: string }, after = 0): string {
  const path = run.events_url ?? `/agent/runs/${run.id}/events`;
  return `${BASE}${path}${after ? `?after=${encodeURIComponent(after)}` : ""}`;
}

// ============================ SSE URL helpers（配合 lib/sse.ts 使用）============================

/** 跨城全量规划流（GET，decision.restart_stream=true 时消费）。 */
export function streamUrl(planId: string): string {
  return `${BASE}/plans/${encodeURIComponent(planId)}/stream`;
}

/** 跨城深研续流（POST，decision.auto_stream=true 时消费，options 需 { method: "POST" }）。 */
export function researchMoreUrl(planId: string): string {
  return `${BASE}/plans/${encodeURIComponent(planId)}/research-more`;
}

/** 房间推荐流（GET，房间 RECOMMENDING 状态下消费）。 */
export function recommendUrl(roomId: number | string): string {
  return `${BASE}/rooms/${roomId}/recommend`;
}

/** 房间 AI 修改流（POST，body 为 RoomModifyBody；事件见 DD-18 §8）。 */
export function modifyPlanUrl(roomId: number | string): string {
  return `${BASE}/rooms/${roomId}/plan/modify`;
}

// ============================ 跨城计划（plans 侧 JSON 端点）============================

/** CreatePlanBody（app.py）。 */
export interface CreatePlanBody {
  constraints?: Record<string, unknown>;
  party?: Record<string, unknown>[];
  organizer_user_id?: number | null;
}

export interface CreatePlanResponse {
  plan_id: string;
  stream: string;
}

/** `POST /plans`：直接建 plan（一般对话首条自动建，此函数供显式创建入口用）。 */
export function createPlan(body: CreatePlanBody = {}): Promise<CreatePlanResponse> {
  return request<CreatePlanResponse>("/plans", jsonOptions("POST", body));
}

/** `GET /plans/{id}/state`：LangGraph state values（bundle/transport_options/candidate_cities 等）。 */
export function getPlanState(planId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/plans/${encodeURIComponent(planId)}/state`);
}

/**
 * `GET /plans/{id}/bundle`：从 trip_bundles 表恢复探索版/确认版（state values 不含大对象，
 * 页面刷新后 /state 拿不到 bundle 时用本端点兜底）。键为 "explore" | "confirm"。
 */
export function getPlanBundle(
  planId: string,
): Promise<Partial<Record<"explore" | "confirm", Record<string, unknown>>>> {
  return request(`/plans/${encodeURIComponent(planId)}/bundle`);
}

/** ImportBody（app.py）：粘贴文本初抽（raw）或确认回填（extracted 覆盖）。 */
export interface BookingImportBody {
  kind: "train" | "flight" | "hotel";
  input_kind?: "text" | "image" | "link" | "manual";
  extracted?: Record<string, unknown>;
  raw?: string;
}

/** 回填确认态（DD-10）：关键字段齐全才 confirmed=true（ready_for_resume）。 */
export interface BookingImportResponse {
  plan_id: string;
  ready_for_resume: boolean;
  booking: {
    kind?: string;
    input_kind?: string;
    extracted?: Record<string, unknown>;
    confirmed?: boolean;
    evidence?: Record<string, unknown>;
  };
}

/** `POST /plans/{id}/bookings/import`（BYO Booking：识别结果须经用户确认）。 */
export function importBooking(
  planId: string,
  body: BookingImportBody,
): Promise<BookingImportResponse> {
  return request<BookingImportResponse>(
    `/plans/${encodeURIComponent(planId)}/bookings/import`,
    jsonOptions("POST", body),
  );
}

/** ReviseBody（app.py）：values 局部覆盖 state，from_node 指定重跑起点。 */
export interface ReviseBody {
  values: Record<string, unknown>;
  from_node?: string;
}

export interface ReviseResponse {
  ok: boolean;
  stage?: string;
}

/** `POST /plans/{id}/revise`：非流式局部修订（时间线微调等）。 */
export function revisePlan(planId: string, body: ReviseBody): Promise<ReviseResponse> {
  return request<ReviseResponse>(
    `/plans/${encodeURIComponent(planId)}/revise`,
    jsonOptions("POST", body),
  );
}

/** ICS 日历订阅链接（DD-13：日历客户端周期 GET 自动刷新）。 */
export function calendarIcsUrl(planId: string): string {
  return `${BASE}/plans/${encodeURIComponent(planId)}/calendar.ics`;
}
