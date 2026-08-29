// lib/types.ts
// 全部共享类型（DD-19 §1.2）。字段与 BFF 实际响应一致：
// 房间部分对齐 src/wheretogo/bff/rooms.py + src/wheretogo/rooms/{service,algorithms}.py，
// 对话部分对齐 src/wheretogo/bff/app.py + src/wheretogo/copilot/handle_turn.py。

// ============================ 证据六态（DD-03）============================

export type VerificationStatus =
  | "confirmed_by_user"
  | "official_source_confirmed"
  | "public_source_observed"
  | "estimated"
  | "unknown"
  | "expired";

export interface Evidence {
  verification_status: string;
  source_url?: string;
  fetched_at?: string;
  source_type?: string;
  note?: string;
}

export interface FieldData {
  value: unknown;
  evidence?: Evidence;
}

// ============================ SSE 卡片负载（DD-13/DD-15）============================

/** node_output / interrupt / done 事件的卡片负载；字段一律经 FactField 渲染。 */
export interface CardPayload {
  /** 来源图节点名（node_output/interrupt/done 事件携带） */
  node?: string;
  /** 卡片负载数据（Trip Bundle / explore_bundle / 节点输出） */
  data?: Record<string, unknown>;
}

// ============================ 对话 Copilot（POST /plans/{id}/chat）============================

export interface ClarifyQuestion {
  slot: string;
  q: string;
  options?: string[];
}

/** chat 决策响应（BFF app.py `_chat_turn_impl`）。 */
export interface ChatDecision {
  plan_id: string | null;
  intent: string;
  action: string;
  reply: string;
  constraints: Record<string, unknown>;
  constraints_patch: Record<string, unknown> | null;
  ready_to_plan: boolean;
  /** true → 前端应 POST /plans/{pid}/research-more 接续深研流 */
  auto_stream?: boolean;
  /** true → 前端应 GET /plans/{pid}/stream 全量重跑 */
  restart_stream?: boolean;
  booking?: Record<string, unknown> | null;
  pending_clarify?: ClarifyQuestion[];
  /** design_itinerary 意图产出：锚点路线卡 */
  route_plan?: RoutePlan;
}

// ============================ 路线设计（DD-15 v1.1 design_itinerary）============================

export interface RouteSlot {
  kind: "activity" | "meal" | "leg";
  title: string;
  venue?: string;
  start: string;
  end: string;
  note?: string;
  evidence?: Evidence;
}

export interface RouteDay {
  date: string;
  slots: RouteSlot[];
}

export interface RoutePlan {
  days: RouteDay[];
  warnings: string[];
  anchors_resolved: number;
  anchors_pending: string[];
  evidence_note?: string;
}

// ============================ 房间（DD-18，BFF rooms.py）============================

/** 与 src/wheretogo/enums.py RoomStatus 一致（大写值）。 */
export type RoomStatus =
  | "DRAFT"
  | "COLLECTING"
  | "THEME_SELECTING"
  | "RECOMMENDING"
  | "ACTIVITY_SELECTED"
  | "PLANNING"
  | "PUBLISHED"
  | "EXPIRED";

/** `GET /rooms/{id}` 的 room 对象（BFF `_room_dict`）。 */
export interface Room {
  id: number;
  status: RoomStatus;
  /** ISO 日期 "2026-08-01" */
  activity_date: string;
  city: string;
  time_window: { earliest?: string; latest?: string } | null;
  budget_range: { min?: number; max?: number; currency?: string } | null;
  theme: string | null;
  theme_method: "direct" | "vote" | "ai" | "wheel" | null;
  invite_code: string;
  created_at: string | null;
  expire_at: string | null;
  /** 创建者 member_id（用于邀请页区分发起人/被邀请人身份） */
  creator_member_id: number | null;
}

/** 房间成员（出口已脱敏：不含坐标与 member_token）。 */
export interface RoomMember {
  member_id: number;
  nickname: string;
  origin_name: string | null;
  earliest_depart: string | null;
  latest_end: string | null;
  /** 人均预算（分） */
  budget: number | null;
  interests: string[];
  hard_constraints: string[];
  negative_prefs: string[];
  transport_pref: "walk" | "transit" | "drive" | "any" | null;
  submitted: boolean;
}

/** `POST /rooms` 响应。 */
export interface CreateRoomResponse {
  room_id: number;
  invite_code: string;
  invite_url: string;
  member_id: number;
  member_token: string;
  status: RoomStatus;
}

/** `GET /rooms/by-invite/{code}` 响应。 */
export interface RoomByInviteResponse {
  room: Room;
}

/** `GET /rooms/{id}` 响应。 */
export interface RoomDetailResponse {
  room: Room;
  members: RoomMember[];
}

/** `POST /rooms/{id}/members` 响应。 */
export interface JoinRoomResponse {
  member_id: number;
  member_token: string;
}

// ---------------- 汇总（GET /rooms/{id}/summary）----------------

export interface CommonWindow {
  start: string | null;
  end: string | null;
  available_hours: number | null;
  feasible: boolean;
  suggestions: string[];
}

/** summary 中的成员子集（脱敏：不出坐标/预算明细）。 */
export interface SummaryMember {
  member_id: number;
  nickname: string;
  origin_name: string | null;
  earliest_depart: string | null;
  latest_end: string | null;
  interests: string[];
  submitted: boolean;
}

export interface RoomSummary {
  room_id: number;
  status: RoomStatus;
  members: SummaryMember[];
  submitted_count: number;
  common_window: CommonWindow;
  interests: string[];
  negative_prefs: string[];
  hard_constraints: string[];
  /** 最小成员预算（分），无则 null */
  budget_min: number | null;
  conflicts: { theme: string; reason: string }[];
  theme_candidates: string[];
}

// ---------------- 主题投票 / 转盘 ----------------

/** `POST /rooms/{id}/theme/vote` 响应；tally 按总分降序。 */
export interface VoteResponse {
  ok: boolean;
  tally: { theme: string; score: number }[];
}

/** `POST /rooms/{id}/theme/wheel` 响应（服务端权威结果，DD-19 §3.4）。 */
export interface WheelResult {
  theme: string;
  weights: { theme: string; weight: number }[];
  spins_left: number;
  excluded: string[];
}

/** `POST /rooms/{id}/theme/confirm` 响应。 */
export interface ConfirmThemeResponse {
  ok: boolean;
  theme: string | null;
  status: RoomStatus;
}

// ---------------- 推荐 / 选活动 ----------------

/** 活动候选（SSE `activity_candidates` / `interrupt` 负载；`_cand_dict` + 排序分项）。 */
export interface ActivityCandidate {
  id: number | string;
  title: string;
  venue: string | null;
  category: string | null;
  price_text: string | null;
  booking_url: string | null;
  start_at: string | null;
  end_at: string | null;
  verification_status: string;
  /** [lng, lat] */
  location: [number, number] | null;
  evidence?: Evidence;
  match_score?: number;
  commute_fairness?: number;
  /** 各成员通勤分钟数（与成员顺序一致） */
  commute_times?: number[];
}

/** `POST /rooms/{id}/select-activity` 响应。 */
export interface SelectActivityResponse {
  ok: boolean;
  room_id: number;
  itinerary_version?: number;
  status?: RoomStatus;
  error?: string;
}

// ---------------- 路线 / 行程 ----------------

/** 成员路线（plan_gathering 节点产出）。 */
export interface MemberRoute {
  member_id: number;
  nickname: string;
  transport_mode: string;
  duration_min: number;
  distance_m?: number | null;
  estimate: boolean;
  deeplink?: string;
  note?: string;
}

export interface GatheringDeparture {
  member_id: number;
  nickname: string;
  suggested_departure: string;
  estimated_arrival: string;
  duration_min: number;
  transport_mode: string;
}

export interface Gathering {
  gathering_point: {
    name: string;
    type: "entrance" | "metro" | "venue";
    coords?: [number, number] | null;
  } | null;
  /** ISO 时间，可能为 null（活动无开始时间时） */
  target_time: string | null;
  member_departures: GatheringDeparture[];
}

/** 行程节点（generate_itinerary 产出；time/meal 节点结构相同）。 */
export interface ItineraryNode {
  type: "gathering" | "activity" | "dining" | string;
  title: string;
  venue?: string | null;
  location?: [number, number] | null;
  /** ISO 时间 */
  start?: string | null;
  end?: string | null;
  booking_url?: string | null;
  point_type?: string;
  note?: string;
  evidence?: Evidence;
}

/** 房间行程负载（`GET /rooms/{id}/plan` 的 itinerary 字段）。 */
export interface Itinerary {
  room_id?: number;
  theme?: string | null;
  activity_date?: string;
  nodes: ItineraryNode[];
  gathering?: Gathering | null;
  member_routes?: MemberRoute[];
  common_time_window?: CommonWindow | null;
  warnings?: string[];
}

/** `GET /rooms/{id}/routes` 响应。 */
export interface RoomRoutesResponse {
  gathering: Gathering | null;
  member_routes: MemberRoute[];
}

/** `GET /rooms/{id}/plan` / `POST /rooms/{id}/plan/undo` 响应。 */
export interface RoomPlanResponse {
  version: number | null;
  itinerary: Itinerary;
}

// ---------------- 分享（GET /rooms/{id}/share，服务端已脱敏）----------------

export interface SharePayload {
  room_id: number;
  city: string;
  activity_date: string;
  theme: string | null;
  status: RoomStatus;
  members: { nickname: string }[];
  itinerary: Record<string, unknown> | null;
}

// ---------------- 前端视图模型（DD-19 §3.6 VerticalTimeline）----------------

/** 时间轴槽位：由 ItineraryNode / Gathering 映射而来，供 VerticalTimeline 渲染。 */
export interface TimelineSlot {
  seq: number;
  kind: "transport" | "activity" | "meal" | "gathering" | "free";
  title: string;
  start_at: FieldData;
  end_at?: FieldData;
  duration_min?: number;
  transport_mode?: string;
  note?: string;
  /** 高德 URI 深链用的地点名 */
  poi_name?: string;
}

// ============================ 跨城 Trip Bundle（DD-13 §3.3）============================

/** 探索版核心活动（core_activities 项；事实字段一律 FieldData 经 FactField）。 */
export interface BundleActivity {
  title?: FieldData;
  venue?: FieldData;
  start_at?: FieldData;
  price_text?: FieldData;
  booking_url?: FieldData;
  /** DD-13 §7.4：静态地图缩略图 + 高德深链（不嵌交互地图） */
  map?: { static_img_url?: string | null; amap_url?: string | null };
}

/** 探索版待确认清单项（结构性文案，非事实字段）。 */
export interface TodoItem {
  kind?: string;
  text: string;
  done?: boolean;
}

/** 探索版块（version=explore 必有；confirm 保留作概览）。 */
export interface ExploreBlock {
  destination?: FieldData;
  /** 结构性文案 */
  theme?: string;
  recommended_transport?: { mode?: FieldData; reason?: string };
  depart_window?: FieldData;
  return_window?: FieldData;
  /** value 形如 {min, max, currency}（分享卡可能被替换为脱敏字符串） */
  budget_band?: FieldData;
  core_activities?: BundleActivity[];
  lodging_area?: FieldData;
  transport_compare?: TransportOptions;
  todo_checklist?: TodoItem[];
}

/** 费用条目（confirmed_cost/estimated_cost；金额单位分）。 */
export interface CostItem {
  label: string;
  amount_cents?: number | null;
  evidence?: Evidence;
}

export interface CostBlock {
  items?: CostItem[];
  total_cents?: number | null;
}

/** 确认版逐小时时间线条目（透传 DD-12 timeline_slots）。 */
export interface BundleTimelineEntry {
  seq?: number;
  kind?: string;
  title?: string;
  start_at?: FieldData;
  end_at?: FieldData;
  ref?: { table?: string; id?: number };
}

export interface RiskItem {
  level?: string;
  text: string;
  evidence?: Evidence;
}

export interface AlternativeItem {
  for?: string;
  text: string;
  evidence?: Evidence;
}

/** 确认版块（version=confirm 才有，已过 DD-03 闸三最终闸）。 */
export interface ConfirmBlock {
  activities?: BundleActivity[];
  dining?: Record<string, unknown>[];
  local_routes?: Record<string, unknown>[];
  timeline?: BundleTimelineEntry[];
  confirmed_cost?: CostBlock;
  estimated_cost?: CostBlock;
  risks?: RiskItem[];
  alternatives?: AlternativeItem[];
}

/** trip_bundles.payload 顶层信封（explore 与 confirm 共用外层）。 */
export interface TripBundle {
  schema_version?: string;
  plan_id?: string;
  version?: "explore" | "confirm" | string;
  generated_at?: string;
  stage?: string;
  /** 结构性文案，非事实字段 */
  title?: string;
  summary?: string;
  share?: { shareable?: boolean; desensitized?: boolean };
  explore?: ExploreBlock;
  confirm?: ConfirmBlock;
  reminders_preview?: { title?: string; body?: string; type?: string }[];
  disclaimer?: string;
  // —— 以下为后端 compose 实际产出的探索版顶层键（DD-13 §5/§6，联调实测 plan 2205）——
  theme?: string;
  cities?: CandidateCity[];
  transport?: TransportOptions;
  activities?: RawExploreActivity[];
  budget_range?: { min?: number | null; max?: number | null; note?: string; evidence?: Evidence; per_person?: boolean };
  lodging_area?: { name?: string | null; note?: string; evidence?: Evidence };
  time_windows?: { depart?: string; return?: string; evidence?: Evidence };
  research_outcome?: string;
  pending_checklist?: string[];
  warnings?: string[];
}

/** 探索版活动（后端实际负载）：纯文本字段 + 活动级 evidence（非逐字段 FieldData）。 */
export interface RawExploreActivity {
  id?: number | string;
  title?: string;
  venue?: string;
  start_at?: string;
  end_at?: string;
  category?: string;
  price_text?: string;
  booking_url?: string;
  location?: string | null;
  evidence?: Evidence;
}

// ============================ 交通（DD-09 §3.2-3.6 transport_options）============================

export interface TransportLeg {
  seg?: string;
  label?: string;
  minutes?: number;
  source?: string;
  note?: string;
}

/** 门到门 DTO（一种交通方式的逐段拆解；任何段都不含票价/余票）。
 *  键名以后端实际产出为准（src/wheretogo/domain/transport.py）：
 *  total_min / run_min / effective_play_min（不是 *_minutes）。 */
export interface DoorToDoor {
  mode?: string;
  station?: string;
  dest_station?: string;
  legs?: TransportLeg[];
  total_min?: number;
  run_min?: number;
  buffer?: { ingress?: number; egress?: number };
  face_minutes?: number;
  depart_at?: string;
  arrive_at?: string;
  effective_play_min?: number;
  /** 每段 Fact.evidence（rule→estimated） */
  evidence_by_seg?: Record<string, Evidence>;
}

export interface PresaleReminder {
  city?: string;
  route?: string;
  train_window?: string;
  open_at?: string;
  station?: string;
  /** 起售状态文案（open_at 已过 → "已起售，请直接购买"） */
  note?: string;
  disclaimer?: string;
  evidence?: Evidence;
}

/** 铁路查询策略卡（PRD 5.4；只有查询策略+官方入口，不做交易）。 */
export interface RailStrategyCard {
  title?: string;
  mode_verdict?: string;
  suggest_queries?: string[];
  why?: string[];
  same_city_stations?: string[];
  official_entry?: {
    url?: string;
    prefill?: Record<string, unknown>;
    honest_note?: string;
  };
  presale?: PresaleReminder[];
  disclaimer?: string;
}

export interface FlightScheduleItem {
  flight_no?: string;
  dep_airport?: string;
  arr_airport?: string;
  dep_time?: string;
  arr_time?: string;
  on_time_rate?: number;
  evidence?: Evidence;
}

/** 航班策略卡（PRD 5.5；时刻为公开观测，不含实时价）。 */
export interface FlightStrategyCard {
  title?: string;
  suggest_windows?: string[];
  airport_compare?: { origin?: string[]; dest?: string[] };
  checklist?: string[];
  price_entry_note?: string;
  schedules?: FlightScheduleItem[];
}

export interface TransportCandidate {
  city?: string;
  city_code?: string;
  /** rail | air | compare | local（后端 DD-09 decide_mode / 同城候选实际产出） */
  recommended_mode?: string;
  reason?: string;
  door_to_door?: { rail?: DoorToDoor; air?: DoorToDoor };
  plans?: Record<string, unknown>[];
  rail_strategy?: RailStrategyCard;
  flight_strategy?: FlightStrategyCard;
  no_ticket_strategy?: Record<string, unknown>;
}

export interface TransportOptions {
  generated_at?: string;
  origin?: string;
  candidates?: TransportCandidate[];
  prefill?: Record<string, unknown>;
  presale?: PresaleReminder[];
  disclaimer?: string;
  degraded?: boolean;
}

// ============================ 候选城市卡（DD-08 destination_discovery 产出）============================

/**
 * node_output discover 的 candidate_cities 项：
 * name/reason 为纯文案 + 卡片级 evidence；量化字段为 {value, evidence} 包装。
 */
export interface CandidateCity {
  city_code?: string;
  name?: string;
  score?: number;
  /** [lng, lat] */
  center?: [number, number] | null;
  reason?: string;
  driven_by_activities?: FieldData;
  recommended_transport?: FieldData;
  effective_play?: FieldData;
  budget_estimate?: FieldData;
  risks?: FieldData;
  evidence?: Evidence;
  map?: { static_img_url?: string | null; amap_url?: string | null };
}

// ============================ 对话消息视图模型（DD-19 §3.2）============================

/** ChatPanel 消息；cards 为 node_output/interrupt/done 的卡片负载，经 CardRouter 渲染。 */
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  cards?: CardPayload[];
  /** clarify 事件的选项（可点选直接回复，DD-19 §4.2） */
  options?: string[];
  /** 流式中（StreamingText 逐字 reveal） */
  streaming?: boolean;
  timestamp: string;
}

// ============================ v4 Agent 回合状态机（/agent/*，v4 §6）============================

/** 澄清请求（bff/agent_api.py `_clarification_dict`）：blocking=false 不阻塞任务。 */
export interface AgentClarification {
  id: string;
  blocking: boolean;
  question: string;
  reason?: string;
  requested_facts?: { name?: string; reason?: string }[];
  assumptions_if_skipped?: string[];
  status?: string;
}

/** Run 视图（`_run_dict`）：events_url 为 SSE 事件流相对路径。 */
export interface AgentRunView {
  id: string;
  status: string;
  type?: string;
  goal?: string;
  parent_run_id?: string | null;
  assumptions?: string[];
  events_url?: string;
  /** workspace 恢复时附带 */
  heartbeat_at?: string | null;
  recent_events?: AgentRunEvent[];
}

/** Run 事件（agent/events.py `event_dict`）：sequence 单调递增，供 Last-Event-ID 续传。 */
export interface AgentRunEvent {
  event_id?: string;
  run_id?: string;
  sequence?: number;
  type?: string;
  phase?: string;
  message?: string;
  payload?: Record<string, unknown> & {
    final?: boolean;
    count?: number;
    status?: string;
    progress?: { completed?: number; total?: number };
  };
  created_at?: string;
  /** 终态补发事件直接带 final/status（agent_api.py 历史数据路径） */
  final?: boolean;
  status?: string;
}

/** 一轮对话响应（transaction.py `_response`）；202=RUNNING、200=终态。 */
export interface AgentTurnResponse {
  plan_id: string;
  conversation_id: string;
  turn_id: string;
  turn_status: string;
  assistant_message: { content: string };
  run: AgentRunView | null;
  clarification: AgentClarification | null;
  error: { message?: string; recovery?: string } | null;
  idempotent: boolean;
  booking?: Record<string, unknown> | null;
  /** design_itinerary 无 Run 时的同步路线卡（DD-15 v1.1） */
  route_plan?: RoutePlan;
  /** 旧字段适配器（过渡观察用，新前端不依赖） */
  reply?: string;
  auto_stream?: boolean;
  restart_stream?: boolean;
  ready_to_plan?: boolean;
}

/** workspace 会话条目（agent/workspace.py）。 */
export interface WorkspaceConversationTurn {
  role: "user" | "assistant";
  content: string;
  intent?: string;
  turn_status?: string;
  route_plan?: RoutePlan;
}

/** 完整工作区快照（GET /agent/conversations/{id}/workspace，v4 §11.3）。 */
export interface AgentWorkspace {
  plan_id: string;
  stage?: string;
  constraints: Record<string, unknown>;
  conversation: WorkspaceConversationTurn[];
  active_turn: {
    id: string;
    sequence_no?: number;
    status: string;
    user_message?: string;
    visible_reply?: string | null;
    error_code?: string | null;
    run_id?: string | null;
    clarification_id?: string | null;
  } | null;
  active_run: AgentRunView | null;
  open_clarifications: AgentClarification[];
  current_plan: Record<string, unknown> | null;
  research_workspace?: Record<string, unknown>;
  last_event_id: number;
}
