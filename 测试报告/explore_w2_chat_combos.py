# 探索性测试 W2：对话组合路径（意图切换/反悔/组合）
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
results = []


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('| ' + detail) if detail else ''}", flush=True)


c = httpx.Client(base_url=BASE, timeout=180)

# 1 部分约束 → 应追问出发地
d = c.post("/plans/new/chat", json={"message": "下周末想去杭州"}).json()
pid = d["plan_id"]
step("轮1: 部分约束建 plan", bool(pid), f"pid={pid} pending={[q['slot'] for q in d.get('pending_clarify', [])]}")

# 2 补全 → 应触发规划流
d = c.post(f"/plans/{pid}/chat", json={"message": "从上海出发，两个人"}).json()
step("轮2: 补全后 ready/restart", d.get("ready_to_plan") or d.get("restart_stream"),
     f"patch={json.dumps(d.get('constraints_patch'), ensure_ascii=False)[:80]}")

# 3 反悔改目的地
d = c.post(f"/plans/{pid}/chat", json={"message": "算了，改去苏州"}).json()
patch = d.get("constraints_patch") or {}
step("轮3: 改目的地苏州", patch.get("target_city_name") == "苏州",
     f"patch={json.dumps(patch, ensure_ascii=False)[:80]}")

# 4 问价格（ask_info）
d = c.post(f"/plans/{pid}/chat", json={"message": "万兽之王演唱会门票多少钱"}).json()
step("轮4: ask_info 有回答", bool(d.get("reply")) and len(d["reply"]) > 5,
     f"intent={d.get('intent')} reply={d['reply'][:60]}")

# 5 雨天方案（weather intent）
d = c.post(f"/plans/{pid}/chat", json={"message": "改成雨天方案"}).json()
step("轮5: 雨天回复不崩溃", bool(d.get("reply")), f"intent={d.get('intent')} reply={d['reply'][:50]}")

# 6 点名锚点设计路线
d = c.post(f"/plans/{pid}/chat",
           json={"message": "既要去动漫博物馆也要去看万兽之王，帮我设计一条路线"}).json()
step("轮6: design_itinerary 出路线卡", d.get("intent") == "design_itinerary" and bool(d.get("route_plan")),
     f"intent={d.get('intent')}")

# 7 回填无效订单文本
r = c.post(f"/plans/{pid}/bookings/import",
           json={"kind": "train", "input_kind": "text", "raw": "随便写的不像订单"})
step("轮7: 无效订单不崩溃且有引导", r.status_code == 200,
     f"{r.status_code} {r.json().get('booking', {}).get('kind', '?')}")

# 8 ICS 日历
r = c.get(f"/plans/{pid}/calendar.ics")
ics_ok = r.status_code == 200 and "BEGIN:VCALENDAR" in r.text
step("轮8: ICS 可下载且格式正确", ics_ok, f"{r.status_code} len={len(r.text)}")

# 9 state 刷新恢复
r = c.get(f"/plans/{pid}/state")
step("轮9: state 可读", r.status_code == 200, f"{r.status_code}")

# 10 对话历史落库
import os
os.environ.setdefault("PGCLIENTENCODING", "UTF8")
from wheretogo.db import get_session
from wheretogo.models import Plan
with get_session() as s:
    p = s.get(Plan, int(pid))
    conv = p.conversation or []
    step("轮10: 对话历史落库 >= 8 条", len(conv) >= 8, f"len={len(conv)}")

fails = [n for n, ok, _ in results if not ok]
print(f"\n==== W2 汇总: PASS {len(results) - len(fails)}/{len(results)} ====")
if fails:
    print("FAILED:", fails)
sys.exit(1 if fails else 0)
