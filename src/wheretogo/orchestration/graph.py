"""图拓扑与 Planner 服务（DD-02 §4/§6/§7/§8/§9/§11）。

- build_graph：真连依赖边 + 条件边（v1.1 E③ 禁止节点各自 →END）。
- PlannerService：start / resume / replan / revise + SSE 事件流；checkpoint 持久化恢复。
"""
from __future__ import annotations

from collections.abc import Iterator

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ..config import get_settings
from . import nodes
from .state import TripPlanState

_EDGES = [
    ("parse", "discover"),
    ("discover", "research"),
    ("research", "reflect"),
    # reflect → transport | research（条件边，见 route_after_reflect）
    ("transport", "await_booking"),
    ("await_booking", "hotel"),
    ("hotel", "mobility"),
    ("mobility", "dining"),
    ("dining", "weather"),
    ("weather", "timeline"),
    ("timeline", "validate"),
]
_NODES = [
    ("parse", nodes.constraint_parser),
    ("discover", nodes.destination_discovery),
    ("research", nodes.activity_research),
    ("reflect", nodes.activity_reflection),
    ("transport", nodes.transport_strategy),
    ("await_booking", nodes.await_booking_node),
    ("hotel", nodes.hotel_area_planning),
    ("mobility", nodes.local_mobility),
    ("dining", nodes.dining_planning),
    ("weather", nodes.weather_awareness),
    ("timeline", nodes.timeline_solver),
    ("validate", nodes.feasibility_validator),
    ("compose", nodes.plan_composer),
]


_VALIDATE_MAX_ATTEMPTS = 3  # reflow/retransport 熔断上限：超 3 次强制 compose（避免死循环）
_REFLECT_MAX_LOOPS = 3  # 研究回环上限：超 3 次强制进入 transport


def route_after_reflect(state: dict) -> str:
    """对标 Researchify _should_continue：充分→继续下游；不充分且未超限→回环重搜。

    reflect 是唯一的研究充分性判定者；路由只消费它写出的显式决定。
    不能直接检查 research_feedback：反馈会在 reflect 中被消费并清空，
    若据此路由会出现“生成了新查询，却没有真正执行”的假多轮。
    """
    loop = state.get("research_loop_count", 0)
    should_continue = state.get("research_should_continue")
    # 兼容旧 checkpoint：它没有显式字段，只能以未消费反馈作为迁移兜底。
    if should_continue is None:
        should_continue = bool(state.get("research_feedback")) and loop < _REFLECT_MAX_LOOPS
    # 新状态以 reflect 的显式判断为权威。reflect 在达到上限时会写 False 并
    # 同时产出 no_better_alternatives；这里再次按 loop 截断会让最后一次查询
    # 永远没执行，终态错误停在 searching。
    if should_continue:
        return "research"
    return "transport"


def route_after_validate(state: dict) -> str:
    """条件边：ok→compose；返程太赶→重挑交通；其它硬冲突→重排时间线（DD-02 §4）。

    熔断：attempts 超上限 → 强制 ok（出确认版 + warning），杜绝 RETURN_TIGHT/reflow 死循环。
    """
    v = state.get("validation") or {}
    attempts = (v.get("metrics") or {}).get("attempts", 1)
    if attempts >= _VALIDATE_MAX_ATTEMPTS:
        return "ok"
    if v.get("ok"):
        return "ok"
    if "RETURN_TIGHT" in v.get("issues", []):
        return "retransport"
    return "reflow"


def build_graph(checkpointer):
    g = StateGraph(TripPlanState)
    for name, fn in _NODES:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    for a, b in _EDGES:
        g.add_edge(a, b)
    # 研究迭代条件回环（对标 Researchify reflect → generate_queries 回环）
    g.add_conditional_edges(
        "reflect",
        route_after_reflect,
        {"research": "research", "transport": "transport"},
    )
    g.add_conditional_edges(
        "validate",
        route_after_validate,
        {"ok": "compose", "reflow": "timeline", "retransport": "transport"},
    )
    g.add_edge("compose", END)
    return g.compile(checkpointer=checkpointer)


def make_postgres_checkpointer():
    """PostgresSaver，表建在独立 `langgraph` schema（DD-01 §9.3）。"""
    from psycopg import Connection
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    s = get_settings()
    conn = Connection.connect(
        s.psycopg_dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={s.db_langgraph_schema}",
    )
    cp = PostgresSaver(conn)
    cp.setup()
    return cp


def _initial_state(
    plan_id: str,
    constraints: dict,
    party: list[dict] | None,
    conversation: list[dict] | None = None,
) -> dict:
    # 初始化 reducer(add) 列表字段为空，标量字段给默认，避免首轮汇聚报错
    return {
        "plan_id": plan_id,
        "stage": "explore",
        "constraints": constraints,
        "party": party or [],
        "candidate_cities": [],
        "origin": {},
        "activities": [],
        "local_routes": [],
        "warnings": [],
        "errors": [],
        "bookings": [],
        "weather": {},
        "replan_reason": None,
        # v2 对话/记忆/深研初值（DD-15/16/17）
        "conversation": list(conversation or []),
        "pending_clarify": [],
        "intent": None,
        "granularity": None,
        "memory_ctx": {},
        "research": {},
        # 研究迭代初值（对标 Researchify）
        "research_history": [],
        "research_loop_count": 0,
        "research_feedback": None,
        "shown_activity_ids": [],
        "shown_activity_titles": [],
        "follow_up_queries": [],
        "research_should_continue": False,
        "research_active_feedback": None,
        "research_baseline_activities": [],
        "research_round_candidates": [],
        "research_raw_candidates": [],
        "research_judged_candidates": [],
        "research_selection": {},
        "plan_selected_candidates": [],
        "research_improved": None,
        "research_personalized": False,
        "research_outcome": None,
        "research_quality": {},
        "research_semantic_evaluation": {},
        "research_stop_reason": None,
        "research_budget_started_at": None,
        "research_budget_exhausted": False,
        "research_revision_mode": "initial",
        "research_artifacts": [],
        "assistant_response": "",
        "itinerary_draft": [],
        "plan_ledger": {},
        "plan_delta": {},
        "research_context": {},
    }


def _interrupt_payload(result: dict) -> dict | None:
    intr = result.get("__interrupt__") if isinstance(result, dict) else None
    if not intr:
        return None
    first = intr[0]
    return getattr(first, "value", first)


class PlannerService:
    """对 BFF 暴露的编排门面（DD-02 §11）。默认内存 checkpointer；生产传 Postgres。"""

    def __init__(self, checkpointer=None):
        self.checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self.graph = build_graph(self.checkpointer)

    def _config(self, plan_id: str, thread_id: str | None = None) -> dict:
        """返回 checkpoint 配置。

        `plans.thread_id` 是规划版本的权威标识。约束变化时 BFF 会生成新 thread_id；
        此处必须尊重它，否则新一轮仍会读到旧 checkpoint，造成活动/告警串轮。
        """
        return {"configurable": {"thread_id": thread_id or f"plan:{plan_id}"}}

    def _current_interrupt(self, plan_id: str, thread_id: str | None = None) -> dict | None:
        """从 checkpoint 快照读取挂起的中断（跨 langgraph 版本的稳健兜底）。"""
        snap = self.graph.get_state(self._config(plan_id, thread_id))
        for task in getattr(snap, "tasks", ()):
            interrupts = getattr(task, "interrupts", ()) or ()
            if interrupts:
                return getattr(interrupts[0], "value", None)
        return None

    # —— 阻塞式（测试/内部）——
    def start(
        self, plan_id: str, constraints: dict, party: list[dict] | None = None,
        *, thread_id: str | None = None,
        conversation: list[dict] | None = None,
    ) -> dict:
        cfg = self._config(plan_id, thread_id)
        result = self.graph.invoke(
            _initial_state(plan_id, constraints, party, conversation),
            cfg,
        )
        payload = _interrupt_payload(result) or self._current_interrupt(plan_id, thread_id)
        return {"state": result, "interrupt": payload}

    def resume(
        self, plan_id: str, bookings: list[dict], *, thread_id: str | None = None
    ) -> dict:
        result = self.graph.invoke(Command(resume=bookings), self._config(plan_id, thread_id))
        payload = _interrupt_payload(result) or self._current_interrupt(plan_id, thread_id)
        return {"state": result, "interrupt": payload}

    def replan(
        self, plan_id: str, reason: str, from_node: str, *, thread_id: str | None = None
    ) -> dict:
        cfg = self._config(plan_id, thread_id)
        self.graph.update_state(cfg, {"replan_reason": reason}, as_node=from_node)
        return {"state": self.graph.invoke(None, cfg)}

    def revise(
        self, plan_id: str, values: dict, from_node: str, *, thread_id: str | None = None
    ) -> dict:
        cfg = self._config(plan_id, thread_id)
        self.graph.update_state(cfg, values, as_node=from_node)
        return {"state": self.graph.invoke(None, cfg)}

    def prepare_research_more(
        self,
        plan_id: str,
        feedback: str,
        *,
        constraints: dict | None = None,
        thread_id: str | None = None,
        revision_mode: str = "alternative",
        conversation: list[dict] | None = None,
        plan_ledger: dict | None = None,
        itinerary_draft: list[dict] | None = None,
    ) -> None:
        """从既有 research 输出进入 reflect，再按新查询回环。

        `as_node="research"` 很关键：下一节点必须先是 reflect，让反馈转成
        follow_up_queries；若伪装成 reflect 输出，图会先拿旧查询再搜一次。
        """
        from ..schemas.constraints import build_rerank_query

        cfg = self._config(plan_id, thread_id)
        values: dict = {
            "research_feedback": feedback,
            "research_loop_count": 0,
            "research_should_continue": False,
            "research_active_feedback": None,
            "research_baseline_activities": [],
            "research_round_candidates": [],
            "research_raw_candidates": [],
            "research_judged_candidates": [],
            "research_selection": {},
            "plan_selected_candidates": [],
            "research_improved": None,
            "research_personalized": False,
            "research_outcome": "searching",
            "research_quality": {},
            "research_semantic_evaluation": {},
            "research_stop_reason": None,
            "research_budget_started_at": None,
            "research_budget_exhausted": False,
            "research_revision_mode": revision_mode,
        }
        if constraints is not None:
            updated_constraints = dict(constraints)
            # 反馈文本和结构化软偏好共同构成新的检索语义，因此缓存键必然变化。
            updated_constraints["query"] = build_rerank_query({
                **updated_constraints,
                "query": feedback,
            })
            values["constraints"] = updated_constraints
        if conversation is not None:
            values["conversation"] = list(conversation)
        if plan_ledger is not None:
            values["plan_ledger"] = dict(plan_ledger)
        if itinerary_draft is not None:
            values["itinerary_draft"] = list(itinerary_draft)
        self.graph.update_state(
            cfg,
            values,
            as_node="research",
        )

    def get_state(self, plan_id: str, *, thread_id: str | None = None):
        return self.graph.get_state(self._config(plan_id, thread_id))

    # —— 流式（SSE，DD-02 §11 事件 schema）——
    def stream_start(
        self, plan_id: str, constraints: dict, party: list[dict] | None = None,
        *, thread_id: str | None = None,
        conversation: list[dict] | None = None,
    ) -> Iterator[dict]:
        yield from self._stream(
            _initial_state(plan_id, constraints, party, conversation),
            self._config(plan_id, thread_id),
        )

    def stream_resume(
        self, plan_id: str, bookings: list[dict], *, thread_id: str | None = None
    ) -> Iterator[dict]:
        yield from self._stream(Command(resume=bookings), self._config(plan_id, thread_id))

    def stream_replan(
        self, plan_id: str, reason: str, from_node: str, *, thread_id: str | None = None
    ) -> Iterator[dict]:
        cfg = self._config(plan_id, thread_id)
        self.graph.update_state(cfg, {"replan_reason": reason}, as_node=from_node)
        yield from self._stream(None, cfg)

    def stream_research_more(
        self, plan_id: str, *, thread_id: str | None = None
    ) -> Iterator[dict]:
        yield from self._stream(None, self._config(plan_id, thread_id))

    def _stream(self, inp, cfg) -> Iterator[dict]:
        try:
            saw_interrupt = False
            for item in self.graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
                mode, data = item if isinstance(item, tuple) and len(item) == 2 else ("updates", item)
                if mode == "custom":  # 节点内实时进度（research 深研）→ 对话可见
                    yield {"event": "progress", "node": "research", "data": data}
                    continue
                chunk = data
                if "__interrupt__" in chunk:
                    intr = chunk["__interrupt__"][0]
                    yield {"event": "interrupt", "node": "await_booking",
                           "data": getattr(intr, "value", intr)}
                    saw_interrupt = True
                    return
                for node, update in chunk.items():
                    event = "done" if node == "compose" else "node_output"
                    yield {"event": event, "node": node, "data": update}
            if not saw_interrupt:  # 版本差异兜底：流结束但存在挂起中断
                snap = self.graph.get_state(cfg)
                for task in getattr(snap, "tasks", ()):
                    interrupts = getattr(task, "interrupts", ()) or ()
                    if interrupts:
                        yield {"event": "interrupt", "node": "await_booking",
                               "data": getattr(interrupts[0], "value", None)}
                        return
        except Exception as e:  # 节点内异常 → error 事件（不静默）
            yield {"event": "error", "node": "graph", "data": {"message": str(e), "degraded": True}}
