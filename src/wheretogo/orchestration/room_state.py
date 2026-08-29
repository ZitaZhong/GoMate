"""DD-18 §3.1 RoomState：市内活动房间子图状态（与 TripPlanState 并存、互不复用）。"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class RoomState(TypedDict, total=False):
    # —— 标识与阶段 ——
    room_id: int
    status: str  # 当前房间状态（RoomStatus 8 态）
    city: str
    city_code: str
    activity_date: str  # ISO date
    # —— 输入（API 阶段写入）——
    members: list[dict]  # 所有成员信息（含坐标，仅图内使用；出口脱敏）
    time_window: dict | None  # 房间级时间窗（创建者设定）
    budget_range: dict | None
    theme: str | None  # 确定的主题
    theme_method: str | None
    theme_candidates: list[dict]  # 候选主题（带权重）
    # —— 节点产物 ——
    common_time_window: dict | None  # collect_members 写
    activity_candidates: list[dict]  # research/rank 写（含通勤矩阵得分）
    selected_activity: dict | None  # confirm_activity resume 写
    gathering: dict | None  # plan_gathering 写：集合点/时间
    member_routes: list[dict]  # plan_gathering 写：每人路线
    itinerary: dict | None  # generate_itinerary 写：当前行程
    itinerary_version: int  # publish 写：版本号
    research: dict  # DD-17 深研元数据（job_id/降级标志）
    weather: dict  # 天气上下文（转盘/排序适配）
    # —— 横切 ——
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[dict], operator.add]
