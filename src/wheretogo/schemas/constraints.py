"""约束（Constraints）契约模型 —— DD-01 §8.1 的权威 schema。

`plans.constraints` 由 DD-07 聚合产出、DD-02 状态机首节点读、DD-05 检索消费。
状态机中以 `dict` 流转（DD-02 §3），本模型用于校验与序列化互转。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BudgetBand(BaseModel):
    min: int | None = None  # 单位元
    max: int | None = None


class Constraints(BaseModel):
    """聚合后的匿名约束。字段与 DD-01 §8.1 JSONB 逐一对应。"""

    party_size: int = 1
    origins: list[str] = Field(default_factory=list)  # 商圈级，脱敏
    earliest_depart: datetime | None = None  # 各人最晚能走的（取 max）
    latest_return: datetime | None = None  # 各人最早要回的（取 min）
    budget_band: BudgetBand = Field(default_factory=BudgetBand)  # 区间交集
    accept_flight: bool = True
    accept_night_train: bool = False
    interests: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    experience_requirements: list[str] = Field(default_factory=list)
    research_goal: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    research_subgoals: list[dict] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list)  # 忌讳
    hard_constraints: list[str] = Field(default_factory=list)
    query: str = ""  # 供 DD-05 检索的自然语言查询

    def to_state_dict(self) -> dict:
        """序列化为状态机使用的 dict（时间转 ISO 字符串）。"""
        data = self.model_dump()
        for k in ("earliest_depart", "latest_return"):
            v = getattr(self, k)
            data[k] = v.isoformat() if v else None
        return data


def build_rerank_query(constraints: dict) -> str:
    """把用户约束 + 偏好拼成重排 query，实现个性化排序（DD-05 §6）。

    只拼真实存在的信号；无兴趣/忌讳等个性化信号时返回空串——
    调用方据此跳过 rerank（保留结构化过滤 + 时间窗），不产出"周末不限 忌讳无"式废话串。
    开放语义：requirements 优先取 experience_requirements，tail 优先取 research_goal。
    """
    requirements = "、".join(
        str(value)
        for value in (
            constraints.get("experience_requirements")
            or constraints.get("interests")
            or []
        )
    )
    soft_preferences = "、".join(constraints.get("soft_preferences") or [])
    dietary = "、".join(constraints.get("dietary") or [])
    budget = constraints.get("budget_band") or {}
    parts: list[str] = []
    if requirements:
        parts.append(f"周末{requirements}")
    if soft_preferences:
        parts.append(f"偏好{soft_preferences}")
    if isinstance(budget, dict) and (budget.get("min") or budget.get("max")):
        parts.append(f"预算{budget.get('min', '')}-{budget.get('max', '')}元")
    if dietary:
        parts.append(f"忌讳{dietary}")
    tail = (
        constraints.get("research_goal")
        or constraints.get("query")
        or ""
    ).strip()
    if tail:
        parts.append(tail)
    return " ".join(parts)
