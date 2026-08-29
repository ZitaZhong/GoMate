"""证据（Evidence）与事实（Fact）契约模型。

对应 DD-01 §5（evidence JSONB 标准结构）与 v1.1 增补 D（PydanticAI 承载 Fact/Evidence）。
所有“对外事实”字段都应携带 Evidence；Provenance Guard（DD-03）读写此结构。
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..enums import SourceType, VerificationStatus

# —— 证据可信度分层（增补 B.1 前端六态可视 + DD-03 Guard 判定的共用基准）——
#: 用户/官方确认级：可作为“已确认”展示
CONFIRMED_STATUSES: frozenset[VerificationStatus] = frozenset(
    {VerificationStatus.confirmed_by_user, VerificationStatus.official_source_confirmed}
)
#: 明确“待确认/不可信”级：前端必须与已确认一眼可辨（PRD 硬 KPI）
UNCERTAIN_STATUSES: frozenset[VerificationStatus] = frozenset(
    {VerificationStatus.estimated, VerificationStatus.unknown, VerificationStatus.expired}
)


class Evidence(BaseModel):
    """标准化证据（内嵌于每个对外事实字段旁，读写零 JOIN）。"""

    model_config = ConfigDict(use_enum_values=False)

    source_type: SourceType
    source_url: str | None = None
    fetched_at: datetime | None = None
    verification_status: VerificationStatus
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    note: str | None = None

    @property
    def is_confirmed(self) -> bool:
        """是否可作为“已确认”对外展示（用户确认或官方源确认）。"""
        return self.verification_status in CONFIRMED_STATUSES

    @property
    def is_uncertain(self) -> bool:
        """是否属于“待确认/不可信”（前端须与已确认视觉区分）。"""
        return self.verification_status in UNCERTAIN_STATUSES

    def to_jsonb(self) -> dict:
        """落库为 DD-01 §5 规范的 JSONB（枚举转字符串、时间转 ISO）。"""
        return {
            "source_type": self.source_type.value,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "verification_status": self.verification_status.value,
            "confidence": self.confidence,
            "note": self.note,
        }

    # —— 降级/兜底常用工厂（DD-02/DD-05 降级路径产出“明确 unknown/estimated”）——
    @classmethod
    def estimated(cls, source_type: SourceType = SourceType.llm, note: str | None = None) -> "Evidence":
        return cls(source_type=source_type, verification_status=VerificationStatus.estimated,
                   confidence=0.4, note=note)

    @classmethod
    def unknown(cls, source_type: SourceType = SourceType.llm, note: str | None = None) -> "Evidence":
        return cls(source_type=source_type, verification_status=VerificationStatus.unknown,
                   confidence=0.0, note=note)


T = TypeVar("T")


class Fact(BaseModel, Generic[T]):
    """一个“对外事实” = 值 + 证据。PydanticAI 抽取子调用的 output_type 目标。"""

    value: T
    evidence: Evidence
