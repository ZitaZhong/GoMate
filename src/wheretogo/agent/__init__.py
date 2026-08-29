"""v4 回合状态机与任务生命周期运行时（技术方案 v4）。

Turn Transaction + Agent Run 执行闭环：模型理解开放世界，确定性运行时验证动作
契约并安全执行。对外入口：commit_turn / execute_run / poll_once / get_workspace。
"""
from .decision import (
    ContractViolation,
    RunDraft,
    TurnResult,
    build_runtime_decision,
    validate_turn_contract,
)
from .metrics import collect_metrics
from .prerequisites import (
    CAPABILITY_SPECS,
    PrerequisiteResolution,
    ToolInputSpec,
    derive_known_facts,
    resolve_prerequisites,
)
from .status import (
    ErrorCode,
    RunStatus,
    RunType,
    TurnStatus,
    can_transition_run,
    can_transition_turn,
    user_facing_error,
)
from .supervisor import execute_run
from .transaction import commit_turn
from .worker import poll_once, scan_stalled
from .workspace import get_workspace

__all__ = [
    "CAPABILITY_SPECS",
    "ContractViolation",
    "ErrorCode",
    "PrerequisiteResolution",
    "RunDraft",
    "RunStatus",
    "RunType",
    "ToolInputSpec",
    "TurnResult",
    "TurnStatus",
    "build_runtime_decision",
    "can_transition_run",
    "can_transition_turn",
    "collect_metrics",
    "commit_turn",
    "derive_known_facts",
    "execute_run",
    "get_workspace",
    "poll_once",
    "resolve_prerequisites",
    "scan_stalled",
    "user_facing_error",
    "validate_turn_contract",
]
