"""DD-18 RoomPlanGraph：市内活动房间子图（与 DD-02 TripPlanGraph 并存）。

thread_id = 'room:{id}'；共享 Postgres checkpointer（独立 langgraph schema）；
interrupt 停在 confirm_activity（等用户/投票选定活动），resume 续跑
plan_gathering → generate_itinerary → publish。
"""
from __future__ import annotations

from collections.abc import Iterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from . import room_nodes
from .room_state import RoomState

_NODES = [
    ("collect_members", room_nodes.collect_members),
    ("select_theme", room_nodes.select_theme),
    ("research_activities", room_nodes.research_activities),
    ("rank_activities", room_nodes.rank_activities),
    ("confirm_activity", room_nodes.confirm_activity),
    ("plan_gathering", room_nodes.plan_gathering),
    ("generate_itinerary", room_nodes.generate_itinerary),
    ("publish", room_nodes.publish),
]

_EDGES = [
    ("collect_members", "select_theme"),
    ("select_theme", "research_activities"),
    ("research_activities", "rank_activities"),
    ("rank_activities", "confirm_activity"),
    ("confirm_activity", "plan_gathering"),
    ("plan_gathering", "generate_itinerary"),
    ("generate_itinerary", "publish"),
]


def build_room_graph(checkpointer):
    g = StateGraph(RoomState)
    for name, fn in _NODES:
        g.add_node(name, fn)
    g.add_edge(START, "collect_members")
    for a, b in _EDGES:
        g.add_edge(a, b)
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)


def _initial_state(room: dict, members: list[dict]) -> dict:
    """初始化 reducer 列表字段为空，避免首轮汇聚报错（同 DD-02 范式）。"""
    return {
        "room_id": room["id"],
        "status": room["status"],
        "city": room.get("city") or "上海",
        "city_code": room.get("city_code") or "",
        "activity_date": room["activity_date"],
        "members": members,
        "time_window": room.get("time_window"),
        "budget_range": room.get("budget_range"),
        "theme": room.get("theme"),
        "theme_method": room.get("theme_method"),
        "theme_candidates": [],
        "activity_candidates": [],
        "selected_activity": None,
        "gathering": None,
        "member_routes": [],
        "itinerary": None,
        "itinerary_version": 0,
        "research": {},
        "weather": {},
        "warnings": [],
        "errors": [],
    }


class RoomPlannerService:
    """对 BFF 暴露的房间编排门面（仿 PlannerService）。默认内存 checkpointer。"""

    def __init__(self, checkpointer=None):
        self.checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self.graph = build_room_graph(self.checkpointer)

    def _config(self, room_id: int | str) -> dict:
        return {"configurable": {"thread_id": f"room:{room_id}"}}

    def get_state(self, room_id: int | str):
        return self.graph.get_state(self._config(room_id))

    # —— 流式（SSE）——
    def stream_recommend(self, room: dict, members: list[dict]) -> Iterator[dict]:
        """启动推荐流：collect → theme → research → rank → interrupt(选活动)。"""
        yield from self._stream(_initial_state(room, members), self._config(room["id"]))

    def stream_select(self, room_id: int | str, selected: dict) -> Iterator[dict]:
        """选定活动后续流：gathering → itinerary → publish。"""
        yield from self._stream(Command(resume=selected), self._config(room_id))

    def _stream(self, inp, cfg) -> Iterator[dict]:
        try:
            saw_interrupt = False
            for item in self.graph.stream(inp, cfg, stream_mode=["updates", "custom"]):
                mode, data = (
                    item if isinstance(item, tuple) and len(item) == 2 else ("updates", item)
                )
                if mode == "custom":  # 节点内实时进度（深研）
                    yield {"event": "progress", "node": "research_activities", "data": data}
                    continue
                chunk = data
                if "__interrupt__" in chunk:
                    intr = chunk["__interrupt__"][0]
                    yield {"event": "interrupt", "node": "confirm_activity",
                           "data": getattr(intr, "value", intr)}
                    saw_interrupt = True
                    return
                for node, update in chunk.items():
                    event = "done" if node == "publish" else "node_output"
                    yield {"event": event, "node": node, "data": update}
            if not saw_interrupt:  # 版本差异兜底：流结束但存在挂起中断
                snap = self.graph.get_state(cfg)
                for task in getattr(snap, "tasks", ()):
                    interrupts = getattr(task, "interrupts", ()) or ()
                    if interrupts:
                        yield {"event": "interrupt", "node": "confirm_activity",
                               "data": getattr(interrupts[0], "value", None)}
                        return
        except Exception as e:  # 节点内异常 → error 事件（不静默）
            yield {"event": "error", "node": "room_graph",
                   "data": {"message": str(e), "degraded": True}}
