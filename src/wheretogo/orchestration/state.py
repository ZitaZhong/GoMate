"""编排状态 `TripPlanState`（DD-02 §3，权威定义，含 reducer）。

reducer 铁律（v1.1 E①）：所有可能被并行节点写的列表字段用 `Annotated[list, operator.add]`，
否则并行汇聚丢更新；dict/标量字段默认 LastValue（覆盖写），由单一 owner 节点写。

v2 增补（DD-15/16/17）：增 conversation/intent/granularity/pending_clarify/memory_ctx/research，
用于对话式 Copilot、跨会话记忆注入与实时深度研究。
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict


class ConversationTurn(TypedDict, total=False):
    """单轮对话（DD-15；持久化兜底存 plans.conversation，主存 checkpoint）。"""

    role: Literal["user", "assistant"]  # 谁说的
    content: str  # 文本内容（解释性文字；事实只在卡片里）
    intent: str | None  # 该轮意图（provide_constraints/clarify_answer/...）
    cards: list[dict]  # 该轮附带的卡片（城市/活动/交通/时间线，每字段带 evidence）


class TripPlanState(TypedDict, total=False):
    # —— 标识与阶段 ——
    plan_id: str
    stage: Literal["explore", "await_booking", "confirm"]  # 与 DD-01 plan_stage 一致
    # —— 输入（DD-07 写）——
    constraints: dict  # DD-01 §8.1 constraints schema（聚合后的匿名约束）
    party: list[dict]  # 各同行人脱敏约束（仅聚合展示）
    # —— 探索阶段产物 ——
    candidate_cities: Annotated[list[dict], operator.add]  # DD-08 写；并行扇出追加
    origin: dict  # DD-08 写：出发城市 {city_code, name, center}（C4：判同城/算门到门）
    activities: list[dict]  # DD-05 research 节点写入（re-research 时替换而非追加）
    transport_options: dict  # DD-09 写：门到门比较 + 策略卡 + 深链 + 起售提醒
    # —— 回填（DD-10 在中断期写入，resume 注入）——
    bookings: list[dict]  # 已确认的车次/航班/酒店（confirmed_by_user）
    # —— 确认阶段产物 ——
    hotel_area: dict  # DD-11 写
    local_routes: Annotated[list[dict], operator.add]  # DD-11 写
    dining: list[dict]  # DD-11 写
    timeline: list[dict]  # DD-12 写
    validation: dict  # DD-12 写：硬约束校验结果
    weather: dict  # DD-12 写：天气顾问（adverse/indoor_pref/source；恶劣天气偏好室内）
    bundle: dict  # DD-13 写：explore/confirm 版 Trip Bundle
    # —— v2 对话/记忆/深研（DD-15/16/17）——
    conversation: Annotated[list[dict], operator.add]  # 多轮消息追加（ConversationTurn）
    intent: str | None  # 当前轮意图（DD-15 classify_intent）
    granularity: Literal["coarse", "medium", "fine"] | None  # 改动粒度（决定重算范围）
    pending_clarify: list[dict]  # 待澄清问题（≤4 题；多轮可反复）
    memory_ctx: dict  # DD-16 注入的长期偏好/历史（会话首 load_memory）
    research: dict  # DD-17 深研元数据（job_id/进度/活动 ids/降级标志）
    # —— 研究迭代（对标 Researchify reflect-loop）——
    research_history: Annotated[list[dict], operator.add]  # 每轮反思累积追加
    research_loop_count: int  # 研究轮次（递增，上限熔断）
    research_feedback: str | None  # 用户反馈文本（处理后清空）
    shown_activity_ids: list[int]  # 已展示过的活动 IDs（排除用）
    shown_activity_titles: list[str]  # 跨来源实体排重；累计整个会话，不只保留最近一轮
    follow_up_queries: list[str]  # gap-driven 追加查询
    research_should_continue: bool  # reflect 的显式路由决定，避免“生成了新查询却不执行”
    research_active_feedback: str | None  # 整个迭代周期持续携带，直到找到更优结果/熔断
    research_baseline_activities: list[dict]  # 用户反馈前的可用方案，补搜全失败时兜底
    research_round_candidates: list[dict]  # 本次反馈周期内跨查询累积的新候选
    research_raw_candidates: list[dict]  # 本轮研究原始候选；评审/生成失败时也不得丢失
    research_judged_candidates: list[dict]  # 带 matched/rejected/unknown 语义状态的候选
    research_selection: dict  # baseline/fresh 选择明细及子目标覆盖
    plan_selected_candidates: list[dict]  # 当前计划实际采用的候选；activities 的新权威来源
    research_improved: bool | None  # 相对 baseline 是否已有新候选
    research_personalized: bool  # 无新增时是否已按新增软偏好重新排序/标注 baseline
    research_outcome: Literal[
        "initial", "searching", "improved", "reranked", "partial_unverified",
        "unchanged", "no_supported_match", "no_better_alternatives",
        "provider_unavailable", "budget_exhausted"
    ] | None
    research_quality: dict  # 实体/证据/来源/覆盖/边际收益的结构化充分性
    research_semantic_evaluation: dict  # 原始目标/验收标准与候选的语义匹配结果
    research_stop_reason: str | None  # quality_sufficient|max_loops|budget_exhausted|...
    research_budget_started_at: str | None  # 整个反馈回环共享的总预算起点
    research_budget_exhausted: bool
    research_revision_mode: Literal["initial", "extend", "replace", "alternative"]
    research_artifacts: Annotated[list[dict], operator.add]
    assistant_response: str
    itinerary_draft: list[dict]
    plan_ledger: dict
    plan_delta: dict
    research_context: dict
    # —— 横切 ——
    warnings: Annotated[list[str], operator.add]  # 多节点追加
    errors: Annotated[list[dict], operator.add]  # 节点级错误（降级用）
    replan_reason: str | None  # 重规划触发原因（weather/info_change/manual）
