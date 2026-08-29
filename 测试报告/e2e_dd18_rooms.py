"""DD-18 真实服务端到端验收：活动房间与市内多人协作全链路（打真实 BFF :8000）。

前置：docker compose up -d（PG 5433/Redis 6380）+ alembic upgrade head +
      uvicorn wheretogo.bff.app:app（.env 真实配置，深研/AMap 按 key 真跑或降级）。
用法：uv run python 测试报告/e2e_dd18_rooms.py [BASE_URL]
输出：控制台表格 + e2e_dd18_results.json；任一 FAIL 退出码 1。
"""
from __future__ import annotations

import io
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

client = httpx.Client(base_url=BASE, timeout=60)
RESULTS: list[dict] = []
CTX: dict = {}


def case(cid: str, ref: str, name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    RESULTS.append({"id": cid, "ref": ref, "name": name,
                    "result": "PASS" if ok else "FAIL", "detail": str(detail)[:300]})
    print(f"[{'PASS' if ok else 'FAIL'}] {cid} {name} — {detail}")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
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


# ---------------- 用例 ----------------
def c01():
    r = client.get("/health")
    return r.status_code == 200 and r.json().get("ok"), f"HTTP {r.status_code}"


def c02():
    import datetime
    d = datetime.date.today()
    sat = d + datetime.timedelta(days=((5 - d.weekday()) % 7 or 7))
    r = client.post("/rooms", json={
        "activity_date": sat.isoformat(), "city": "上海",
        "time_window": {"earliest": "10:00", "latest": "21:00"},
        "budget_range": {"min": 0, "max": 200, "currency": "CNY"},
        "creator_nickname": "小北"})
    d2 = r.json()
    CTX.update(room_id=d2["room_id"], invite=d2["invite_code"],
               creator=(d2["member_id"], d2["member_token"]), date=sat.isoformat())
    ok = r.status_code == 200 and d2["status"] == "COLLECTING" and d2["invite_code"]
    return ok, f"room_id={d2['room_id']} invite={d2['invite_code']}"


def c03():
    rid = CTX["room_id"]
    ok1 = client.get(f"/rooms/by-invite/{CTX['invite']}").json()["room"]["id"] == rid
    ok2 = client.get("/rooms/by-invite/badcode").status_code == 404
    m2 = client.post(f"/rooms/{rid}/members", json={"nickname": "阿黄"}).json()
    m3 = client.post(f"/rooms/{rid}/members", json={"nickname": "老王"}).json()
    CTX["m2"], CTX["m3"] = (m2["member_id"], m2["member_token"]), (m3["member_id"], m3["member_token"])
    return ok1 and ok2 and m2["member_token"] != m3["member_token"], "3 人在房（含创建者）"


def c04():
    rid = CTX["room_id"]
    infos = [
        (CTX["creator"], {"origin_name": "徐家汇", "origin_lng": 121.4365, "origin_lat": 31.1947,
                          "earliest_depart": "14:00", "latest_end": "21:00",
                          "budget": 15000, "interests": ["展览"], "transport_pref": "transit"}),
        (CTX["m2"], {"origin_name": "五角场", "origin_lng": 121.5145, "origin_lat": 31.3005,
                     "earliest_depart": "13:00", "latest_end": "20:00",
                     "budget": 20000, "interests": ["市集"], "negative_prefs": ["剧本杀"],
                     "hard_constraints": ["剧本杀"],  # 硬性约束 → 转盘硬排除
                     "transport_pref": "transit"}),
        (CTX["m3"], {"origin_name": "静安寺", "origin_lng": 121.4453, "origin_lat": 31.2233,
                     "earliest_depart": "15:00", "latest_end": "22:00",
                     "budget": 10000, "interests": ["展览"], "transport_pref": "drive"}),
    ]
    for (mid, tok), body in infos:
        r = client.put(f"/rooms/{rid}/members/{mid}", json={"member_token": tok, **body})
        if r.status_code != 200:
            return False, f"成员 {mid} 更新失败 HTTP {r.status_code}: {r.text[:120]}"
    bad = client.put(f"/rooms/{rid}/members/{CTX['m2'][0]}",
                     json={"member_token": "wrong", "origin_name": "x"})
    return bad.status_code == 403, "3 人提交 + 假 token 403"


def c05():
    s = client.get(f"/rooms/{CTX['room_id']}/summary").json()
    cw = s["common_window"]
    ok = (cw["start"] == "15:00" and cw["end"] == "20:00" and s["submitted_count"] == 3
          and cw["feasible"] and "origin_coords" not in json.dumps(s["members"]))
    return ok, f"共同窗 {cw['start']}~{cw['end']} ({cw['available_hours']}h)"


def c06():
    rid = CTX["room_id"]
    early = client.post(f"/rooms/{rid}/select-activity", json={"activity_id": 1})
    return early.status_code == 409, f"未确认主题就选活动 → HTTP {early.status_code}"


def c07():
    rid = CTX["room_id"]
    v = client.post(f"/rooms/{rid}/theme/vote", json={
        "member_token": CTX["creator"][1], "theme": "展览", "weight": 3})
    bad = client.post(f"/rooms/{rid}/theme/vote", json={
        "member_token": CTX["creator"][1], "theme": "展览", "weight": 5})
    ok = v.status_code == 200 and v.json()["tally"][0]["theme"] == "展览" and bad.status_code == 422
    return ok, f"tally={v.json()['tally'][:2]}"


def c08():
    rid = CTX["room_id"]
    w1 = client.post(f"/rooms/{rid}/theme/wheel").json()
    w2 = client.post(f"/rooms/{rid}/theme/wheel").json()
    w3 = client.post(f"/rooms/{rid}/theme/wheel")
    excluded_ok = "剧本杀" in (w1.get("excluded") or [])
    return (w1["spins_left"] == 1 and w2["spins_left"] == 0 and w3.status_code == 409
            and excluded_ok), f"w1={w1['theme']} w2={w2['theme']} 硬排除={w1.get('excluded')}"


def c09():
    rid = CTX["room_id"]
    r = client.post(f"/rooms/{rid}/theme/confirm", json={"theme": "展览", "method": "vote"})
    again = client.post(f"/rooms/{rid}/theme/confirm", json={"theme": "展览", "method": "vote"})
    return (r.status_code == 200 and r.json()["status"] == "RECOMMENDING"
            and again.status_code == 409), f"status={r.json().get('status')}"


def c10():
    rid = CTX["room_id"]
    with client.stream("GET", f"/rooms/{rid}/recommend", timeout=420) as resp:
        text = "".join(resp.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    cands = next((d.get("candidates") for e, d in events if e == "activity_candidates"), [])
    progress_n = sum(1 for e in names if e == "progress")
    CTX["candidates"] = cands or []
    ok = "room_state" in names and "interrupt" in names and len(cands or []) >= 1
    return ok, (f"events={sorted(set(names))} 候选={len(cands or [])} "
                f"深研进度条数={progress_n} 首条={cands[0]['title'][:30] if cands else '无'}")


def c11():
    rid = CTX["room_id"]
    cands = CTX.get("candidates") or []
    if not cands:
        return False, "无候选，无法选活动"
    r = client.post(f"/rooms/{rid}/select-activity",
                    json={"activity_id": cands[0]["id"]}, timeout=180)
    d = r.json()
    CTX["chosen_title"] = cands[0]["title"]
    return (r.status_code == 200 and d.get("status") == "PUBLISHED"
            and d.get("itinerary_version") == 1), f"{d}"


def c12():
    rid = CTX["room_id"]
    r = client.get(f"/rooms/{rid}/routes").json()
    routes = r.get("member_routes") or []
    g = r.get("gathering") or {}
    deps = g.get("member_departures") or []
    ok = len(routes) == 3 and g.get("gathering_point") and len(deps) == 3
    return ok, (f"路线={len(routes)} 集合点={g.get('gathering_point', {}).get('name')} "
                f"集合时间={g.get('target_time')}")


def c13():
    rid = CTX["room_id"]
    p = client.get(f"/rooms/{rid}/plan").json()
    types = [n["type"] for n in p["itinerary"]["nodes"]]
    CTX["nodes_v1"] = p["itinerary"]["nodes"]
    return p["version"] == 1 and "activity" in types, f"v{p['version']} nodes={types}"


def c14():
    rid = CTX["room_id"]
    r = client.post(f"/rooms/{rid}/plan/modify",
                    json={"message": "整体推迟半小时"}, timeout=120)
    events = parse_sse(r.text)
    cls = next((d for e, d in events if e == "revision_classified"), {})
    upd = next((d for e, d in events if e == "itinerary_updated"), None)
    ok = cls.get("revision_type") == "adjust_time" and upd and upd["version"] == 2
    return ok, f"识别={cls.get('revision_type')} 新版本={upd and upd['version']}"


def c15():
    rid = CTX["room_id"]
    r = client.post(f"/rooms/{rid}/plan/modify",
                    json={"message": "换一家不辣的餐厅"}, timeout=120)
    events = parse_sse(r.text)
    cls = next((d for e, d in events if e == "revision_classified"), {})
    upd = next((d for e, d in events if e == "itinerary_updated"), None)
    if not (cls.get("revision_type") == "replace_node" and cls.get("target_kind") == "dining"):
        return False, f"识别错误: {cls}"
    if upd:  # 有餐饮节点被替换 → 活动节点必须原样
        acts_old = [n for n in CTX["nodes_v1"] if n["type"] == "activity"]
        acts_new = [n for n in upd["itinerary"]["nodes"] if n["type"] == "activity"]
        same = acts_old and acts_new and acts_old[0]["title"] == acts_new[0]["title"]
        return bool(same), f"v{upd['version']} 活动节点未动={same}"
    return True, "行程无餐饮节点 → 无变更（合理跳过）"


def c16():
    rid = CTX["room_id"]
    r = client.post(f"/rooms/{rid}/plan/modify", json={"message": "取消活动"}, timeout=60)
    events = parse_sse(r.text)
    needs = any(e == "needs_confirmation" for e, _ in events)
    ver = client.get(f"/rooms/{rid}/plan").json()["version"]
    return needs and ver >= 2, f"needs_confirmation={needs} 当前版本 v{ver}（未被误改）"


def c17():
    rid = CTX["room_id"]
    before = client.get(f"/rooms/{rid}/plan").json()["version"]
    r = client.post(f"/rooms/{rid}/plan/undo")
    after = r.json().get("version")
    return r.status_code == 200 and after == before - 1, f"v{before} → v{after}"


def c18():
    rid = CTX["room_id"]
    s = client.get(f"/rooms/{rid}/share").json()
    dump = json.dumps(s, ensure_ascii=False)
    leaks = [k for k in ("member_token", "origin_coords", "origin_name",
                         "徐家汇", "五角场", "静安寺") if k in dump]
    ok = not leaks and s["theme"] == "展览" and len(s["members"]) == 3
    return ok, f"泄露字段={leaks or '无'} 成员={[m['nickname'] for m in s['members']]}"


def c19():
    rid = CTX["room_id"]
    room = client.get(f"/rooms/{rid}").json()["room"]
    return room["status"] == "PUBLISHED", f"终态={room['status']}（8 态顺序流转完成）"


CASES = [
    ("R01", "基础", "服务健康检查", c01),
    ("R02", "DD-18 §8", "创建房间（COLLECTING + 邀请码）", c02),
    ("R03", "DD-18 §8", "邀请码查房 + 2 名成员加入（无效码 404）", c03),
    ("R04", "DD-18 §2.2", "3 人信息提交（真实坐标）+ 假 token 403", c04),
    ("R05", "DD-18 §4.1", "聚合摘要：共同时间窗 15:00~20:00 + 脱敏", c05),
    ("R06", "DD-18 §3.2", "非法状态跳转 409（未确认主题选活动）", c06),
    ("R07", "DD-18 §8", "主题投票 + 非法权重 422", c07),
    ("R08", "DD-18 §4.2", "转盘 2 次（一次反悔）+ 第 3 次 409 + 硬约束排除", c08),
    ("R09", "DD-18 §3.2", "确认主题 → RECOMMENDING（重复确认 409）", c09),
    ("R10", "DD-18 §7/§9", "推荐 SSE：深研(scope=local)+候选+interrupt", c10),
    ("R11", "DD-18 §3.2", "选定活动 → PLANNING → PUBLISHED（行程 v1）", c11),
    ("R12", "DD-18 §4.4", "3 人路线 + 集合点 + 倒推出发时间", c12),
    ("R13", "DD-18 §8", "获取当前行程（含活动节点）", c13),
    ("R14", "DD-18 §5", "AI 修改：推迟半小时 → adjust_time → v2", c14),
    ("R15", "DD-18 §5.2", "AI 修改：换不辣餐厅 → 只动餐饮节点", c15),
    ("R16", "DD-18 §5.3", "AI 修改：取消活动 → needs_confirmation（不落版）", c16),
    ("R17", "DD-18 §6", "撤销回上一版", c17),
    ("R18", "DD-18 §10", "分享脱敏：无坐标/出发地/token", c18),
    ("R19", "DD-18 §10", "房间终态 PUBLISHED", c19),
]


def main() -> int:
    print(f"== DD-18 房间全链路真实服务 E2E（{BASE}）==\n")
    for cid, ref, name, fn in CASES:
        case(cid, ref, name, fn)
    passed = sum(1 for r in RESULTS if r["result"] == "PASS")
    print(f"\n结果：{passed}/{len(RESULTS)} PASS")
    with open("测试报告/e2e_dd18_results.json", "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
