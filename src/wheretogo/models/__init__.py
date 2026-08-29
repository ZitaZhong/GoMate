"""全部 ORM 模型聚合导出（Alembic 与应用统一从此导入 Base.metadata）。"""

from ..db.base import Base
from .agent import AgentOutbox, AgentRun, AgentRunEvent, AgentTurn, ClarificationRequest
from .city_activity import Activity, CityPlaybook, RawPage, SourceRegistry, Venue
from .room import Room, RoomItinerary, RoomMember, ThemeVote
from .trip import Booking, DiningPick, Reminder, RouteLeg, TimelineSlot, TripBundle
from .user_plan import Plan, PlanMember, PartyConstraint, User, UserContext
from .v2 import ActivityReviewQueue, DeepResearchCache, DeepResearchJob, UserMemory

__all__ = [
    "Base",
    # 用户/计划域
    "User",
    "UserContext",
    "Plan",
    "PlanMember",
    "PartyConstraint",
    # 城市/活动域
    "CityPlaybook",
    "Venue",
    "SourceRegistry",
    "RawPage",
    "Activity",
    # 回填/行程域
    "Booking",
    "DiningPick",
    "RouteLeg",
    "TimelineSlot",
    "TripBundle",
    "Reminder",
    # v2 新增（记忆 / 深研 / 审核队列）
    "UserMemory",
    "DeepResearchJob",
    "DeepResearchCache",
    "ActivityReviewQueue",
    # v4 新增（回合状态机与任务生命周期）
    "AgentTurn",
    "AgentRun",
    "AgentRunEvent",
    "ClarificationRequest",
    "AgentOutbox",
    # DD-18 GoMate 活动房间域
    "Room",
    "RoomMember",
    "ThemeVote",
    "RoomItinerary",
]
