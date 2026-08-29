# DD-19 前端契约视角的活服务联调：DD-18 房间全链路 + DD-15 对话链路
# 只打 http://127.0.0.1:8000，模拟 web-v2 前端的真实调用序列。
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
results = []


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('| ' + detail) if detail else ''}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30.0)

    # 1. 创建房间（组织者）
    r = c.post("/rooms", json={
        "activity_date": "2026-08-01", "city": "上海",
        "time_window": {"earliest": "12:00", "latest": "21:00"},
        "creator_nickname": "老周",
    })
    step("POST /rooms", r.status_code == 200, r.text[:120])
    room = r.json()
    rid = room["room_id"]
    invite_code = room.get("invite_code") or room.get("room", {}).get("invite_code")
    creator_token = room.get("member_token") or room.get("creator", {}).get("member_token")
    creator_id = room.get("member_id") or room.get("creator", {}).get("member_id")
    print("  room keys:", sorted(room.keys()))

    # 2. 邀请码解析
    r = c.get(f"/rooms/by-invite/{invite_code}")
    step("GET /rooms/by-invite/{code}", r.status_code == 200, r.text[:100])

    # 3. 两名成员加入
    members = {}
    for nick in ("小恬", "小林"):
        r = c.post(f"/rooms/{rid}/members", json={"nickname": nick})
        step(f"POST /rooms/{rid}/members ({nick})", r.status_code == 200, r.text[:100])
        body = r.json()
        members[nick] = (body.get("member_id"), body.get("member_token"))
    members["老周"] = (creator_id, creator_token)

    # 4. 三人填写信息（预算单位：分；时间 HH:MM；nickname 在 join 时已定）
    infos = {
        "老周": {"origin_name": "徐家汇", "earliest_depart": "12:00", "latest_end": "20:00",
                 "interests": ["看展", "咖啡探店"], "hard_constraints": [], "budget": 20000,
                 "transport_pref": "transit"},
        "小恬": {"origin_name": "五角场", "earliest_depart": "12:30", "latest_end": "20:00",
                 "interests": ["看展", "咖啡探店"], "hard_constraints": [], "budget": 20000,
                 "transport_pref": "transit"},
        "小林": {"origin_name": "中山公园", "earliest_depart": "13:00", "latest_end": "19:00",
                 "interests": ["做手工"], "hard_constraints": ["不接受户外"], "budget": 15000,
                 "transport_pref": "any"},
    }
    for nick, (mid, token) in members.items():
        payload = {"member_token": token, **infos[nick]}
        r = c.put(f"/rooms/{rid}/members/{mid}", json=payload)
        step(f"PUT member ({nick})", r.status_code == 200, r.text[:80])

    # 5. 聚合摘要
    r = c.get(f"/rooms/{rid}/summary")
    step("GET summary", r.status_code == 200, r.text[:200])
    summary = r.json()
    print("  summary keys:", sorted(summary.keys()))

    # 6. 转盘：第 1、2 次成功，第 3 次 409（一次反悔=最多 2 次）
    wheel = None
    for i in (1, 2, 3):
        r = c.post(f"/rooms/{rid}/theme/wheel")
        if i <= 2:
            step(f"wheel spin #{i}", r.status_code == 200, r.text[:150])
            wheel = r.json()
        else:
            step("wheel spin #3 → 409", r.status_code == 409, r.text[:100])
    print("  wheel keys:", sorted(wheel.keys()), "spins_left:", wheel.get("spins_left"))

    # 7. 确认主题（ConfirmThemeBody 无 member_token）
    r = c.post(f"/rooms/{rid}/theme/confirm",
               json={"theme": wheel["theme"], "method": "wheel"})
    step("POST theme/confirm", r.status_code == 200, r.text[:120])

    # 8. 推荐 SSE：收集事件名与候选
    events_seen = {}
    candidates = []
    with c.stream("GET", f"/rooms/{rid}/recommend", timeout=120) as resp:
        step("GET recommend SSE 200", resp.status_code == 200, f"status={resp.status_code}")
        ev, data_lines = None, []
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and ev is not None:
                raw = "\n".join(data_lines)
                events_seen[ev] = events_seen.get(ev, 0) + 1
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {}
                if ev == "activity_candidates":
                    candidates = payload.get("candidates", [])
                if ev in ("done", "error"):
                    break
                ev, data_lines = None, []
                continue
            continue
    print("  SSE events:", events_seen)
    step("SSE 产生候选活动", len(candidates) > 0, f"candidates={len(candidates)}")
    if candidates:
        print("  candidate[0] keys:", sorted(candidates[0].keys()))

    # 9. 选定活动（SelectActivityBody：activity_id 或 activity dict）
    if candidates:
        aid = candidates[0].get("id") or candidates[0].get("activity_id")
        r = c.post(f"/rooms/{rid}/select-activity", json={"activity_id": aid})
        step("POST select-activity", r.status_code == 200, r.text[:200])
        print("  select resp keys:", sorted(r.json().keys()))

    # 10. 路线 + 行程
    r = c.get(f"/rooms/{rid}/routes")
    step("GET routes", r.status_code == 200, r.text[:200])
    r = c.get(f"/rooms/{rid}/plan")
    step("GET plan", r.status_code == 200, r.text[:200])
    plan = r.json()
    print("  plan keys:", sorted(plan.keys()) if isinstance(plan, dict) else type(plan))

    # 11. AI 修改（ModifyBody：message + confirm）
    r = c.post(f"/rooms/{rid}/plan/modify",
               json={"message": "把咖啡换成可以坐久一点的甜品店"},
               timeout=120)
    step("POST plan/modify", r.status_code == 200, r.text[:200])
    modified = "itinerary_updated" in r.text  # no_change（无餐饮节点）时不落新版

    # 12. 撤销（仅当第 11 步真的落了 v2 才有可撤版本；
    #     no_change 路径下 404「没有可撤销的历史版本」是正确行为，
    #     modify→undo 正向链路由 e2e_dd18_rooms R14+R17 覆盖）
    r = c.post(f"/rooms/{rid}/plan/undo")
    if modified:
        step("POST plan/undo", r.status_code == 200, r.text[:150])
    else:
        step("POST plan/undo（no_change → 404 可读文案）",
             r.status_code == 404 and "可撤销" in r.text, r.text[:150])

    # 13. 分享（脱敏检查）
    r = c.get(f"/rooms/{rid}/share")
    step("GET share", r.status_code == 200, r.text[:300])
    share_text = r.text
    leaked = [w for w in ("经纬", "lng", "lat", "latitude") if w in share_text.lower()]
    step("share 不含坐标字段", not leaked, f"leak={leaked}")

    # 14. 对话链路：chat-first 自动建 plan
    r = c.post("/plans/new/chat",
               json={"message": "这个周末我们想从上海去北京玩两天，三个人，喜欢逛展"},
               timeout=120)
    step("POST /plans/new/chat", r.status_code == 200, r.text[:250])
    decision = r.json()
    print("  decision keys:", sorted(decision.keys()))
    pid = decision.get("plan_id")
    step("chat-first 自动建 plan", bool(pid), f"plan_id={pid}")
    step("decision 含 reply", bool(decision.get("reply")))
    if pid and decision.get("restart_stream"):
        first_events = []
        with c.stream("GET", f"/plans/{pid}/stream", timeout=60) as resp:
            ev = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                    first_events.append(ev)
                if len(first_events) >= 6 or ev == "done":
                    break
        print("  stream first events:", first_events[:6])
        step("plan stream 有事件", len(first_events) > 0)

    fails = [n for n, ok, _ in results if not ok]
    print("\n==== 汇总 ====")
    print(f"PASS {len(results) - len(fails)} / {len(results)}")
    if fails:
        print("FAILED:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
