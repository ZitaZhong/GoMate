// lib/constants.ts
// 标签/快捷指令/主题等常量（DD-19 §3.7、§5.3、§5.5；后端 rooms/algorithms.py）。

// ---- 成员兴趣标签（DD-19 §5.5 / GoMate PRD §7.2.2，快捷标签多选）----
export const INTEREST_TAGS = [
  "看展",
  "公园",
  "Citywalk",
  "做手工",
  "咖啡探店",
  "吃饭聊天",
  "逛市集",
  "看演出",
  "运动",
  "拍照",
  "逛书店",
  "桌游",
  "宠物友好",
  "夜间活动",
] as const;

// ---- 硬约束标签（GoMate PRD §7.2.3，与兴趣分开展示）----
export const HARD_CONSTRAINT_TAGS = [
  "不接受户外",
  "不想走太多路",
  "不吃辣",
  "预算上限",
  "需早睡回家",
  "无障碍",
] as const;

// ---- ModifyInput 快捷指令（DD-19 §3.7 / PRD §7.8.1，七条）----
export const QUICK_COMMANDS = [
  "换一个活动",
  "换一家餐厅",
  "少走一点路",
  "整体晚一小时",
  "控制预算",
  "增加拍照地点",
  "改成雨天方案",
] as const;

// ---- 主题列表（后端 DEFAULT_THEMES，rooms/algorithms.py；候选池还会并入成员兴趣并集）----
export const DEFAULT_THEMES = [
  "展览",
  "演出",
  "市集",
  "户外",
  "美食",
  "桌游",
  "运动",
  "手作",
] as const;

// ---- 出行方式偏好（后端 UpdateMemberBody.transport_pref 校验值）----
export const TRANSPORT_PREFS = [
  { value: "transit", label: "公交地铁" },
  { value: "drive", label: "打车/开车" },
  { value: "walk", label: "步行骑行优先" },
  { value: "any", label: "都行" },
] as const;

// ---- 主题投票权重（后端 VoteBody 校验：仅支持 1/3/-2）----
export const VOTE_WEIGHTS = [
  { weight: 3, label: "强烈喜欢" },
  { weight: 1, label: "可接受" },
  { weight: -2, label: "不喜欢" },
] as const;

// ---- 房间状态 → 路由映射（DD-19 §5.3 路由守卫）----
export const ROOM_STATUS_ROUTES: Record<string, string> = {
  DRAFT: "/invite",
  COLLECTING: "/summary",
  THEME_SELECTING: "/theme",
  RECOMMENDING: "/recommend",
  ACTIVITY_SELECTED: "/activity",
  PLANNING: "/plan",
  PUBLISHED: "/plan",
  EXPIRED: "",
};
