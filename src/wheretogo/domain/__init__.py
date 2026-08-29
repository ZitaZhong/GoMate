"""领域层（DD-07/08/09/10/11/12 的确定性算法；节点 pure-function 委托至此）。"""
from __future__ import annotations

from .backfill import build_resume_payload, confirm_booking, run_extract, to_timeline_anchors
from .compose import (
    build_ics,
    build_ics_fallback,
    build_presale_reminders,
    build_reminders,
    reminders_preview,
    run_final_gate,
)
from .constraints import aggregate_party, intersect_bands, missing_slots, parse_constraints
from .destination import destination_discovery, score_city
from .stay_mobility_dining import plan_dining, plan_hotel_area, plan_local_mobility
from .timeline import solve_timeline, validate_timeline
from .route_design import design_day_route, resolve_anchors
from .transport import (
    BUFFER_RULES_MIN,
    build_12306_entry,
    build_prefill_hints,
    build_transport_options,
    door_to_door,
    estimate_door_to_door,
    presale_open_time,
)

__all__ = [
    # DD-07
    "parse_constraints", "missing_slots", "aggregate_party", "intersect_bands",
    # DD-08
    "destination_discovery", "score_city",
    # DD-09
    "BUFFER_RULES_MIN", "estimate_door_to_door", "door_to_door", "presale_open_time",
    "build_12306_entry", "build_prefill_hints", "build_transport_options",
    # DD-10
    "run_extract", "confirm_booking", "to_timeline_anchors", "build_resume_payload",
    # DD-11
    "plan_hotel_area", "plan_local_mobility", "plan_dining",
    # DD-12
    "solve_timeline", "validate_timeline",
    # DD-13
    "run_final_gate", "build_reminders", "build_presale_reminders", "reminders_preview",
    "build_ics", "build_ics_fallback",
    # DD-15 v1.1 锚点路线设计
    "design_day_route", "resolve_anchors",
]
