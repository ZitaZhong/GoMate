"""DD-16 记忆与个性化：长期语义记忆（user_memory，Mem0 风格 + 覆盖语义）。

会话工作记忆由 LangGraph checkpoint（DD-02 state.conversation）覆盖；本模块专注跨会话长期偏好：
- load_memory：结构化 user_context + 语义 user_memory 召回（仅 valid）。
- write_memory：同 key 软失效覆盖（旧 valid=FALSE）+ 插新；解决 Mem0 ADD-only 旧新共存。
- extract_and_write：LLM 抽稳定偏好（带 key 归一）；无 key → 不写。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db.session import SessionLocal
from ..models import UserContext, UserMemory
from ..providers import extract_json
from ..retrieval.service import RetrievalService

_svc: RetrievalService | None = None


def _svc_get() -> RetrievalService:
    global _svc
    if _svc is None:
        _svc = RetrievalService()
    return _svc


def load_memory(user_id: int, query: str | None = None, top_k: int = 8,
                session: Session | None = None) -> dict:
    """会话开始：返回 {structured: user_context, semantic: [记忆]}。仅 valid=TRUE。"""
    own = session is None
    s = session or SessionLocal()
    try:
        uc = s.get(UserContext, user_id)
        structured = {}
        if uc:
            structured = {"home_cities": uc.home_cities, "interests": uc.interests,
                          "dietary": uc.dietary, "budget_band": uc.budget_band}
        if query:
            emb = _svc_get().embed([query])[0]
            rows = s.execute(text(
                "SELECT mem_type, key, content, confidence FROM user_memory "
                "WHERE user_id = :u AND valid = TRUE "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
            ), {"u": user_id, "v": str(emb), "k": top_k}).all()
        else:
            rows = s.execute(text(
                "SELECT mem_type, key, content, confidence FROM user_memory "
                "WHERE user_id = :u AND valid = TRUE ORDER BY updated_at DESC LIMIT :k"
            ), {"u": user_id, "k": top_k}).all()
        semantic = [{"mem_type": r[0], "key": r[1], "content": r[2], "confidence": r[3]} for r in rows]
        return {"structured": structured, "semantic": semantic}
    finally:
        if own:
            s.close()


def write_memory(user_id: int, mem_type: str, key: str | None, content: str,
                 plan_id: int | None = None, conf: float = 0.7,
                 session: Session | None = None) -> int:
    """同 key 覆盖（旧 valid=FALSE）+ 插新；返回新 memory id。"""
    own = session is None
    s = session or SessionLocal()
    try:
        emb = _svc_get().embed([content])[0]
        if key:  # 同键覆盖：软失效旧记录
            s.execute(text(
                "UPDATE user_memory SET valid = FALSE, updated_at = now() "
                "WHERE user_id = :u AND key = :k AND valid = TRUE"
            ), {"u": user_id, "k": key})
        m = UserMemory(user_id=user_id, mem_type=mem_type, key=key, content=content,
                       embedding=emb, confidence=conf, source_plan_id=plan_id, valid=True)
        s.add(m)
        s.flush()
        if own:
            s.commit()
        return m.id
    finally:
        if own:
            s.close()


def extract_and_write(conversation: list[dict], plan: dict, user_id: int,
                      session: Session | None = None) -> list[int]:
    """LLM 从对话抽稳定偏好（带 key 归一），覆盖写入；无 key → 不写。返回 memory ids。"""
    corpus = " ".join(t.get("content", "") for t in (conversation or []) if t.get("role") == "user")
    parsed = extract_json(
        "memory_extract",
        "抽取用户的稳定偏好（饮食/出发地/预算/兴趣），输出 {memories:[{mem_type,key,content}]}；"
        "只取稳定信号，不记一次性约束",
        corpus,
    )
    items = (parsed or {}).get("memories") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    ids: list[int] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        ids.append(write_memory(user_id, item.get("mem_type", "preference"),
                                item.get("key"), item["content"],
                                plan.get("plan_id"), session=session))
    return ids
