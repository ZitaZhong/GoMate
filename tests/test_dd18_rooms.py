"""DD-18 验收（§10）：房间状态机 / 时间窗 / 转盘 / 公平性 / 集合 / AI 修改 / 版本 / 脱敏。

算法单测为纯函数；API 全流程走 TestClient（离线确定性兜底，见 conftest 强制离线）。
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from wheretogo.bff.app import app
from wheretogo.rooms.algorithms import (
    commute_fairness_score,
    compute_common_window,
    compute_gathering,
    rank_by_fairness,
    weighted_wheel,
)
from wheretogo.rooms.revision import apply_revision, classify_revision

client = TestClient(app)


# ============================ §4.1 共同时间窗 ============================
def test_common_window_three_members_intersection():
    members = [
        {"nickname": "A", "earliest_depart": "14:00", "latest_end": "21:00"},
        {"nickname": "B", "earliest_depart": "13:00", "latest_end": "20:00"},
        {"nickname": "C", "earliest_depart": "15:00", "latest_end": "22:00"},
    ]
    w = compute_common_window(members)
    assert w["start"] == "15:00" and w["end"] == "20:00"
    assert w["available_hours"] == 5.0 and w["feasible"] is True
    assert w["suggestions"] == []


def test_common_window_insufficient_gives_suggestions():
    members = [
        {"nickname": "早退", "earliest_depart": "14:00", "latest_end": "15:30"},
        {"nickname": "晚到", "earliest_depart": "14:30", "latest_end": "21:00"},
    ]
    w = compute_common_window(members)
    assert w["feasible"] is False and w["available_hours"] == 1.0
    assert w["suggestions"]  # 不足 2h 必须给建议
    assert any("早退" in s or "晚到" in s for s in w["suggestions"])


# ============================ §4.2 主题转盘 ============================
def test_wheel_hard_excluded_and_weighting():
    members = [
        {"interests": ["展览"], "negative_prefs": ["运动"]},
        {"interests": ["展览", "市集"], "negative_prefs": []},
    ]
    theme, weights = weighted_wheel(
        ["展览", "市集", "运动", "剧本杀"], members,
        hard_excluded={"剧本杀"}, rng=random.Random(42),
    )
    themes_in_wheel = {w["theme"] for w in weights}
    assert "剧本杀" not in themes_in_wheel  # 硬约束排除
    w_map = {w["theme"]: w["weight"] for w in weights}
    # 展览：3+3+1=7 > 市集：1+3+1=5 > 运动：-2+1+1=0(被过滤) → 权重正确
    assert w_map["展览"] == 7 and w_map["市集"] == 5
    assert "运动" not in w_map  # 权重<=0 不入盘
    assert theme in themes_in_wheel


def test_wheel_all_filtered_falls_back_to_random():
    members = [{"interests": [], "negative_prefs": ["展览", "市集"]}]
    theme, weights = weighted_wheel(
        ["展览", "市集"], members, rng=random.Random(1),
    )
    # 两主题权重 -2+1=-1 ≤0 → 全过滤 → 完全随机降级（weights 为空）
    assert weights == [] and theme in ("展览", "市集")


# ============================ §4.3 通勤公平性 ============================
def test_fairness_ranking_puts_fairest_first():
    members = [{"interests": []}, {"interests": []}, {"interests": []}]
    acts = [{"id": i, "title": f"a{i}", "verification_status": "official_source_confirmed"}
            for i in range(5)]
    matrix = {
        "0": [60, 10, 10],  # 差距大 + 有人 60 分钟
        "1": [25, 25, 25],  # 完全公平
        "2": [40, 40, 40],  # 公平但都远
        "3": [10, 20, 70],  # 有人极远
        "4": [30, 28, 26],  # 较公平
    }
    ranked = rank_by_fairness(acts, members, matrix)
    assert ranked[0]["id"] == 1  # 最公平的排最前
    assert commute_fairness_score([25, 25, 25]) < commute_fairness_score([60, 10, 10])
    fairness_by_id = {a["id"]: a["commute_fairness"] for a in ranked}
    assert fairness_by_id[1] < fairness_by_id[4] < fairness_by_id[0]


# ============================ §4.4 集合点与时间 ============================
def test_gathering_back_calculates_departures_with_buffer():
    activity = {"start_at": "2026-08-01T15:00:00+08:00", "venue": "美术馆"}
    routes = [
        {"member_id": 1, "nickname": "A", "transport_mode": "transit", "duration_min": 30},
        {"member_id": 2, "nickname": "B", "transport_mode": "driving", "duration_min": 20},
    ]
    g = compute_gathering(activity, [], routes)
    assert g["target_time"] == "2026-08-01T14:45:00+08:00"  # 提前 15 分钟
    dep = {d["nickname"]: d for d in g["member_departures"]}
    # A：14:45 - (30 + 公交buffer10) = 14:05；B：14:45 - (20 + 驾车buffer15) = 14:10
    assert dep["A"]["suggested_departure"] == "2026-08-01T14:05:00+08:00"
    assert dep["B"]["suggested_departure"] == "2026-08-01T14:10:00+08:00"
    assert g["gathering_point"]["name"] == "美术馆"  # 无入口/地铁 → 场馆兜底


def test_gathering_point_priority_entrance_over_metro():
    act = {"start_at": "2026-08-01T15:00:00+08:00",
           "entrance_poi": {"name": "西门", "coords": [121.4, 31.2]},
           "nearby_metro": {"name": "陕西南路站", "coords": [121.45, 31.21]}}
    g = compute_gathering(act, [], [])
    assert g["gathering_point"]["type"] == "entrance"


# ============================ §5 AI 修改 ============================
def _itinerary_fixture() -> dict:
    return {
        "room_id": 1, "theme": "展览",
        "nodes": [
            {"type": "gathering", "title": "集合 · 美术馆",
             "start": "2026-08-01T14:45:00+08:00"},
            {"type": "activity", "title": "某某画展", "venue": "美术馆",
             "start": "2026-08-01T15:00:00+08:00", "end": "2026-08-01T17:00:00+08:00"},
            {"type": "dining", "title": "川味火锅", "meal_slot": "dinner"},
        ],
        "member_routes": [{"member_id": 1, "duration_min": 30}],
    }


def test_classify_revision_rule_fallback():
    assert classify_revision("换一家不辣的餐厅")["revision_type"] == "replace_node"
    assert classify_revision("换一家不辣的餐厅")["target_kind"] == "dining"
    assert classify_revision("整体推迟半小时")["revision_type"] == "adjust_time"
    assert classify_revision("全部推倒重来")["revision_type"] == "full_replan"
    fb = classify_revision("嗯呃这个那个")  # 无法识别 → 换一批活动兜底
    assert fb["revision_type"] == "replace_node" and fb["degraded"] is True


def test_replace_dining_only_touches_dining_node():
    it = _itinerary_fixture()
    decision = classify_revision("换一家不辣的餐厅")
    new, changed, confirms = apply_revision(
        it, decision, "换一家不辣的餐厅",
        replacement={"title": "粤式茶餐厅",
                     "evidence": {"verification_status": "public_source_observed"}},
    )
    assert changed == ["川味火锅"] and confirms == []
    nodes = {n["type"]: n for n in new["nodes"]}
    assert nodes["dining"]["title"] == "粤式茶餐厅"  # 只有餐饮变了
    assert nodes["activity"]["title"] == "某某画展"  # 活动不动
    assert nodes["gathering"]["title"] == "集合 · 美术馆"  # 集合不动
    assert new["member_routes"] == it["member_routes"]  # 路线不动（原对象未被改）


def test_remove_activity_needs_confirmation():
    it = _itinerary_fixture()
    new, changed, confirms = apply_revision(
        it, {"revision_type": "remove_node", "target_kind": "activity", "keyword": None},
        "取消活动")
    assert confirms and changed == []
    assert len(new["nodes"]) == 3  # 未真正删除，等确认


def test_adjust_time_shifts_all_and_flags_overrun():
    it = _itinerary_fixture()
    decision = classify_revision("整体推迟半小时")
    new, changed, confirms = apply_revision(
        it, decision, "整体推迟半小时", common_window_end="17:00")
    nodes = {n["type"]: n for n in new["nodes"]}
    assert nodes["activity"]["start"] == "2026-08-01T15:30:00+08:00"
    assert nodes["gathering"]["start"] == "2026-08-01T15:15:00+08:00"
    assert confirms  # 17:30 结束 > 共同窗 17:00 → 需确认


# ============================ API 全流程（离线）============================
def _mk_room(activity_date="2026-07-25") -> dict:
    return client.post("/rooms", json={
        "activity_date": activity_date, "city": "上海",
        "time_window": {"earliest": "10:00", "latest": "21:00"},
        "budget_range": {"min": 0, "max": 200, "currency": "CNY"},
        "creator_nickname": "小北",
    }).json()


def _seed_activities(titles: list[str]) -> list[int]:
    from wheretogo.db import get_session
    from wheretogo.enums import VerificationStatus
    from wheretogo.models import Activity
    from wheretogo.retrieval.providers import HashingEmbeddingProvider

    emb = HashingEmbeddingProvider()
    ids = []
    with get_session() as s:
        for t in titles:
            a = Activity(
                title=t, city_code="310000", venue="测试场馆", category="展览",
                evidence={"source_type": "official_venue",
                          "verification_status": "official_source_confirmed",
                          "confidence": 0.9},
                verification_status=VerificationStatus.official_source_confirmed,
                embedding=emb.embed([f"{t} 测试场馆 展览"])[0],
                start_at=datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc),
                expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
            )
            s.add(a)
            s.flush()
            ids.append(a.id)
    return ids


def _cleanup_activities(ids: list[int]) -> None:
    from sqlalchemy import text
    from wheretogo.db import get_session
    with get_session() as s:
        s.execute(text("DELETE FROM activities WHERE id = ANY(:ids)"), {"ids": ids})


def _parse_sse(text_body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text_body.replace("\r\n", "\n").split("\n\n"):
        ev, data = None, ""
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if ev and data:
            try:
                events.append((ev, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


def test_room_full_flow_offline():
    """8 状态顺序流转 + 推荐 SSE + 选活动 + 路线 + 修改 + 撤销 + 分享脱敏。"""
    seeded = _seed_activities(["DD18测试画展甲", "DD18测试画展乙", "DD18测试市集丙"])
    try:
        created = _mk_room()
        rid = created["room_id"]
        assert created["invite_code"] and created["status"] == "COLLECTING"

        # —— 邀请码查房 + 两名成员加入（共 3 人）——
        by_code = client.get(f"/rooms/by-invite/{created['invite_code']}").json()
        assert by_code["room"]["id"] == rid
        assert client.get("/rooms/by-invite/nope").status_code == 404
        m2 = client.post(f"/rooms/{rid}/members", json={"nickname": "阿黄"}).json()
        m3 = client.post(f"/rooms/{rid}/members", json={"nickname": "老王"}).json()

        # —— 成员填写信息（不同时间窗）——
        creator_token = created["member_token"]
        for mid, tok, dep, end, interests in (
            (created["member_id"], creator_token, "14:00", "21:00", ["展览"]),
            (m2["member_id"], m2["member_token"], "13:00", "20:00", ["市集"]),
            (m3["member_id"], m3["member_token"], "15:00", "22:00", ["展览"]),
        ):
            r = client.put(f"/rooms/{rid}/members/{mid}", json={
                "member_token": tok, "origin_name": "徐家汇",
                "earliest_depart": dep, "latest_end": end,
                "budget": 15000, "interests": interests, "transport_pref": "transit",
            })
            assert r.status_code == 200
        # token 错误 → 403
        assert client.put(f"/rooms/{rid}/members/{m2['member_id']}", json={
            "member_token": "bad", "origin_name": "x"}).status_code == 403

        # —— 摘要：共同窗 15:00-20:00、脱敏（无坐标/无预算明细）——
        summary = client.get(f"/rooms/{rid}/summary").json()
        assert summary["common_window"]["start"] == "15:00"
        assert summary["common_window"]["end"] == "20:00"
        assert summary["submitted_count"] == 3
        assert "origin_coords" not in json.dumps(summary["members"])

        # —— 非法跳转：未确认主题就选活动 → 409 ——
        assert client.post(f"/rooms/{rid}/select-activity",
                           json={"activity_id": 1}).status_code == 409

        # —— 投票 + 转盘（2 次后第 3 次 409）——
        vote = client.post(f"/rooms/{rid}/theme/vote", json={
            "member_token": creator_token, "theme": "展览", "weight": 3}).json()
        assert vote["tally"][0]["theme"] == "展览"
        assert client.post(f"/rooms/{rid}/theme/vote", json={
            "member_token": creator_token, "theme": "展览", "weight": 5}).status_code == 422
        w1 = client.post(f"/rooms/{rid}/theme/wheel").json()
        assert w1["theme"] and w1["spins_left"] == 1
        w2 = client.post(f"/rooms/{rid}/theme/wheel").json()  # 一次反悔
        assert w2["spins_left"] == 0
        assert client.post(f"/rooms/{rid}/theme/wheel").status_code == 409

        # —— 确认主题 → RECOMMENDING ——
        conf = client.post(f"/rooms/{rid}/theme/confirm",
                           json={"theme": "展览", "method": "vote"}).json()
        assert conf["status"] == "RECOMMENDING"
        # 重复确认 → 409
        assert client.post(f"/rooms/{rid}/theme/confirm",
                           json={"theme": "展览", "method": "vote"}).status_code == 409

        # —— 推荐 SSE：candidates + interrupt ——
        resp = client.get(f"/rooms/{rid}/recommend")
        events = _parse_sse(resp.text)
        names = [e for e, _ in events]
        assert "room_state" in names and "interrupt" in names
        cand_ev = next(d for e, d in events if e == "activity_candidates")
        titles = [c["title"] for c in cand_ev["candidates"]]
        assert any("DD18测试" in t for t in titles)
        chosen = next(c for c in cand_ev["candidates"] if "DD18测试" in c["title"])

        # —— 选活动 → PLANNING → PUBLISHED（行程落库）——
        sel = client.post(f"/rooms/{rid}/select-activity",
                          json={"activity_id": chosen["id"]}).json()
        assert sel["ok"] and sel["status"] == "PUBLISHED"
        assert sel.get("itinerary_version") == 1

        # —— 路线与行程 ——
        routes = client.get(f"/rooms/{rid}/routes").json()
        assert len(routes["member_routes"]) == 3
        plan = client.get(f"/rooms/{rid}/plan").json()
        assert plan["version"] == 1
        node_types = [n["type"] for n in plan["itinerary"]["nodes"]]
        assert "activity" in node_types

        # —— AI 修改：推迟半小时（局部更新 → v2）——
        mod = client.post(f"/rooms/{rid}/plan/modify",
                          json={"message": "整体推迟半小时"})
        mod_events = _parse_sse(mod.text)
        assert mod_events[0][0] == "revision_classified"
        assert mod_events[0][1]["revision_type"] == "adjust_time"
        updated = next(d for e, d in mod_events if e == "itinerary_updated")
        assert updated["version"] == 2

        # —— 修改需确认：取消活动 → needs_confirmation（版本不变）——
        mod2 = client.post(f"/rooms/{rid}/plan/modify", json={"message": "取消活动"})
        ev2 = _parse_sse(mod2.text)
        assert any(e == "needs_confirmation" for e, _ in ev2)
        assert client.get(f"/rooms/{rid}/plan").json()["version"] == 2

        # —— 撤销 → 回到 v1 ——
        undo = client.post(f"/rooms/{rid}/plan/undo").json()
        assert undo["version"] == 1
        # 再撤销 → 404（没有更早版本）
        assert client.post(f"/rooms/{rid}/plan/undo").status_code == 404

        # —— 分享脱敏：无精确出发地/坐标/token/预算 ——
        share = client.get(f"/rooms/{rid}/share").json()
        dump = json.dumps(share, ensure_ascii=False)
        assert "member_token" not in dump and "origin_coords" not in dump
        assert "origin_name" not in dump and "徐家汇" not in dump
        assert share["members"] == [{"nickname": n} for n in ("小北", "阿黄", "老王")]
        assert share["itinerary"] is not None

        # —— 房间详情：成员出口无坐标 ——
        room = client.get(f"/rooms/{rid}").json()
        assert room["room"]["status"] == "PUBLISHED"
        assert "origin_coords" not in json.dumps(room["members"])
    finally:
        _cleanup_activities(seeded)


def test_room_versions_keep_five():
    from wheretogo.db import get_session
    from wheretogo.rooms import save_itinerary_version, undo_itinerary

    created = _mk_room(activity_date=date(2026, 8, 1).isoformat())
    rid = created["room_id"]
    with get_session() as s:
        for i in range(1, 8):
            v = save_itinerary_version(s, rid, {"n": i})
        assert v == 7
    from sqlalchemy import text
    with get_session() as s:
        count = s.scalar(text(
            "SELECT count(*) FROM room_itineraries WHERE room_id=:r"), {"r": rid})
        assert count == 5  # 只保留最近 5 版
        assert undo_itinerary(s, rid) == {"n": 6}  # 撤销回上一版
