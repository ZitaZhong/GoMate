"""对话理解的结构化契约。

面向用户的自然语言是开放集合；这里约束的是系统可执行的输出空间，而不是枚举用户说法。
旧版 ``intent/action`` 仍由 ``handle_turn`` 兼容输出，新代码以 acts + commands 为准。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


TurnAct = Literal[
    "update_constraints",
    "research_more",
    "recompose_plan",
    "answer_info",
    "submit_booking",
    "weather_replan",
    "chitchat",
    "clarify",
]
CommandType = Literal[
    "update_constraints",
    "research_more",
    "recompose_plan",
    "answer",
    "submit_booking_draft",
    "request_weather_replan",
    "ask_clarification",
]


class ConstraintOperation(BaseModel):
    """对约束的显式修改，避免把否定/清空含义压扁成一个模糊 patch。"""

    model_config = ConfigDict(extra="forbid")

    op: Literal["set", "add", "remove", "clear"]
    field: str
    value: Any = None


class TurnCommand(BaseModel):
    """BFF/编排层可执行命令；解释层本身不产生外部副作用。"""

    model_config = ConfigDict(extra="forbid")

    type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)


class TurnDecision(BaseModel):
    """一轮对话的权威结构化解释。"""

    model_config = ConfigDict(extra="forbid")

    primary_intent: str
    acts: list[TurnAct] = Field(default_factory=list)
    constraints_patch: dict[str, Any] = Field(default_factory=dict)
    constraint_operations: list[ConstraintOperation] = Field(default_factory=list)
    commands: list[TurnCommand] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    research_goal: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    clarification: str | None = None
    assistant_reply: str | None = None
    itinerary_draft: list[dict[str, Any]] = Field(default_factory=list)
    memory_note: str | None = None
    confidence: float = 0.5
    interpretation_source: Literal["llm", "rules", "hybrid"] = "rules"
    # —— v4 增量字段（可选，旧调用方不受影响）——
    # goals：开放目标 {id, objective, required}；目标语义保持自由文本。
    goals: list[dict[str, Any]] = Field(default_factory=list)
    # proposed_actions：建议动作 {type, reason}；type 属于封闭能力集
    # （research/transport_search/compose_itinerary/answer/booking/replan）。
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)
    # clarification_candidates：候选事实需求 {fact, reason}；是否阻塞由运行时判定。
    clarification_candidates: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("acts")
    @classmethod
    def _dedupe_acts(cls, values: list[TurnAct]) -> list[TurnAct]:
        return list(dict.fromkeys(values))

    @field_validator("confidence")
    @classmethod
    def _bound_confidence(cls, value: float) -> float:
        return min(1.0, max(0.0, value))

    def to_public_dict(self) -> dict[str, Any]:
        """序列化为稳定 JSON；Pydantic 对象不泄漏到 FastAPI 响应。"""
        return self.model_dump(mode="json")
