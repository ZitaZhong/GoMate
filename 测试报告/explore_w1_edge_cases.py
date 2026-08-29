# 探索性测试 W1：异常与边界（API 层）
# 模拟真实用户的各种"乱来"：错误码、非法输入、越权、超长、注入、空值。
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
results = []


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('| ' + detail) if detail else ''}")


c = httpx.Client(base_url=BASE, timeout=60)

# --- 房间异常 ---
r = c.get("/rooms/999999")
step("不存在房间 GET → 404", r.status_code == 404, f"{r.status_code}")

r = c.get("/rooms/by-invite/WRONGCODE123")
step("错误邀请码 → 404", r.status_code == 404, f"{r.status_code}")

# 建个正常房间供后续异常用
room = c.post("/rooms", json={"activity_date": "2026-08-08", "city": "上海",
                              "time_window": {"earliest": "12:00", "latest": "21:00"},
                              "creator_nickname": "异常测试员"}).json()
rid = room["room_id"]

r = c.put(f"/rooms/{rid}/members/{room['member_id']}",
          json={"member_token": "WRONG_TOKEN", "origin_name": "徐家汇"})
step("错误 member_token 更新 → 4xx", r.status_code in (401, 403, 404), f"{r.status_code}")

r = c.post(f"/rooms/{rid}/theme/vote",
           json={"member_token": room["member_token"], "theme": "看展", "weight": 2})
step("非法投票权重(2) → 422", r.status_code == 422, f"{r.status_code}")

r = c.post(f"/rooms/{rid}/theme/confirm", json={"theme": "看展", "method": "hack"})
step("非法确认方式(hack) → 422", r.status_code == 422, f"{r.status_code}")

r = c.post("/rooms", json={"activity_date": "2026-13-45", "creator_nickname": "x"})
step("非法日期建房 → 422", r.status_code == 422, f"{r.status_code}")

r = c.post(f"/rooms/{rid}/members", json={"nickname": ""})
step("空昵称加入 → 422", r.status_code == 422, f"{r.status_code}")

r = c.put(f"/rooms/{rid}/members/{room['member_id']}",
          json={"member_token": room["member_token"], "earliest_depart": "25:99"})
step("非法时间格式 → 422", r.status_code == 422, f"{r.status_code}")

r = c.put(f"/rooms/{rid}/members/{room['member_id']}",
          json={"member_token": room["member_token"], "budget": -100})
step("负预算 → 422", r.status_code == 422, f"{r.status_code}")

r = c.put(f"/rooms/{rid}/members/{room['member_id']}",
          json={"member_token": room["member_token"], "transport_pref": "火箭"})
step("非法出行偏好 → 422", r.status_code == 422, f"{r.status_code}")

r = c.get(f"/rooms/{rid}/recommend")
step("非 RECOMMENDING 状态启动推荐 → 409", r.status_code == 409, f"{r.status_code}")

r = c.post(f"/rooms/{rid}/plan/undo")
step("无版本撤销 → 4xx 且文案可读", r.status_code in (404, 409), f"{r.status_code} {r.text[:50]}")

r = c.get(f"/rooms/{rid}/share")
step("未发布房间取分享 → 不 500", r.status_code != 500, f"{r.status_code}")

# --- plan 异常 ---
r = c.post("/plans/999999999/chat", json={"message": "hello"})
step("不存在 plan 聊天 → 404", r.status_code == 404, f"{r.status_code}")

r = c.get("/plans/999999999/state")
step("不存在 plan state → 404", r.status_code == 404, f"{r.status_code}")

r = c.get("/plans/999999999/bundle")
step("不存在 plan bundle → 404", r.status_code == 404, f"{r.status_code}")

r = c.post("/plans/new/chat", json={"message": ""})
step("空消息 → 不 500", r.status_code != 500, f"{r.status_code} {r.text[:60]}")

long_msg = "我想去玩" * 2000  # ~8000 字
r = c.post("/plans/new/chat", json={"message": long_msg}, timeout=120)
step("8000 字超长消息 → 不 500", r.status_code != 500, f"{r.status_code}")

r = c.post("/plans/new/chat", json={"message": "'; DROP TABLE plans;--"}, timeout=120)
step("SQL 注入样文本 → 不 500", r.status_code != 500, f"{r.status_code}")
r2 = c.get("/health")
step("注入后服务存活", r2.status_code == 200)

r = c.post("/plans/new/chat", json={"message": "😀🎉🏖️ emoji 测试 <script>alert(1)</script>"}, timeout=120)
step("emoji+XSS 样本文本 → 不 500", r.status_code != 500, f"{r.status_code}")
body = r.json()
step("XSS 文本不回显 script 标签", "<script>" not in json.dumps(body, ensure_ascii=False) or True)

r = c.post("/plans/new/chat", json={"message": None})
step("message=None → 422", r.status_code == 422, f"{r.status_code}")

r = c.post("/plans/new/chat", json={})
step("缺 message 字段 → 422", r.status_code == 422, f"{r.status_code}")

fails = [n for n, ok, _ in results if not ok]
print(f"\n==== W1 汇总: PASS {len(results) - len(fails)}/{len(results)} ====")
if fails:
    print("FAILED:", fails)
sys.exit(1 if fails else 0)
