# v4 Agent 活服务 E2E：/agent/* 回合状态机全链路（真实 BFF + Worker + LLM + Tavily）。
#
# 覆盖（对齐 v4 详细设计 §6/§9/§11 与 v4 PRD 报告硬 KPI）：
#   A1 chat-first：POST turns 自动建 plan，202=RUNNING + 真实 run
#   A2 Idempotency-Key 幂等重发 → 同 turn/run
#   A3 run 事件流 SSE：事件单调递增；断开后 after=N 续传不重复不丢
#   A4 run 终态（final）→ workspace：conversation/current_plan/active_run 收敛
#   A5 澄清（若产生）：blocking 语义字段完整
#   A6 取消：新 run 创建后 cancel → 终态 cancelled/终止
#   A7 design_itinerary（v4 语义）：点名排路线 → run（research）或同步路线卡，不留白
#   A8 /agent/metrics 硬 KPI：silent_terminal / promised_without_run / hidden_clarification 均为 0
#
# 用法：uv run python 测试报告/live_e2e_v4_agent.py [BASE_URL]
from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
results: list[tuple[str, bool, str]] = []
c = httpx.Client(base_url=BASE, timeout=120)


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('| ' + str(detail)[:160]) if detail else ''}")


def read_events(run_id: str, after: int = 0, max_seconds: float = 600) -> list[dict]:
    """流式读事件直到 final 或超时；返回事件列表（含 sequence）。"""
    events: list[dict] = []
    deadline = time.monotonic() + max_seconds
    with c.stream("GET", f"/agent/runs/{run_id}/events",
                  params={"after": after}, timeout=max_seconds) as resp:
        ev_type, data = None, ""
        for line in resp.iter_lines():
            if time.monotonic() > deadline:
                break
            if line.startswith("event:"):
                ev_type = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
            elif not line and data:
                try:
                    d = json.loads(data)
                except json.JSONDecodeError:
                    d = {}
                d["_event"] = ev_type
                events.append(d)
                if (d.get("payload") or {}).get("final") or d.get("final"):
                    return events
                ev_type, data = None, ""
    return events


print(f"== v4 Agent 活服务 E2E（{BASE}）==\n")

# ── A1 chat-first turn → run ──
key1 = str(uuid.uuid4())
r = c.post("/agent/conversations/new/turns",
           json={"message": "两个人这周末从上海出发去杭州玩，预算人均800，想看展和逛老街"},
           headers={"Idempotency-Key": key1})
d = r.json()
pid = d.get("plan_id")
step("A1 turns 建 plan + 响应契约", r.status_code in (200, 202)
     and bool(pid) and "assistant_message" in d and "turn_status" in d,
     f"HTTP {r.status_code} plan={pid} status={d.get('turn_status')}")
run = d.get("run") or {}
step("A1 202=RUNNING 时带真实 run", (r.status_code != 202) or bool(run.get("id")),
     f"run={run.get('id')} type={run.get('type')}")

# ── A2 幂等重发 ──
r2 = c.post(f"/agent/conversations/{pid}/turns",
            json={"message": "两个人这周末从上海出发去杭州玩，预算人均800，想看展和逛老街"},
            headers={"Idempotency-Key": key1})
d2 = r2.json()
step("A2 Idempotency-Key 重发同 turn/run",
     d2.get("idempotent") is True and d2.get("turn_id") == d.get("turn_id")
     and (d2.get("run") or {}).get("id") == run.get("id"),
     f"idempotent={d2.get('idempotent')} turn={d2.get('turn_id')}")

# ── A3 事件流 + 续传 ──
seqs: list[int] = []
if run.get("id"):
    events = read_events(run["id"], after=0, max_seconds=600)
    seqs = [e.get("sequence") for e in events if isinstance(e.get("sequence"), int)]
    step("A3 事件流单调递增且见 final",
         len(seqs) >= 2 and seqs == sorted(seqs)
         and any((e.get("payload") or {}).get("final") or e.get("final") for e in events),
         f"events={len(events)} last_seq={seqs[-1] if seqs else None}")
    if len(seqs) >= 3:
        mid = seqs[len(seqs) // 2]
        resumed = read_events(run["id"], after=mid, max_seconds=60)
        rseqs = [e.get("sequence") for e in resumed if isinstance(e.get("sequence"), int)]
        step("A3 after=N 续传不重复不丢",
             bool(rseqs) and min(rseqs) > mid and rseqs == sorted(rseqs)
             and set(rseqs) == {s for s in seqs if s > mid},
             f"after={mid} resumed={len(rseqs)}")
    else:
        step("A3 after=N 续传不重复不丢", False, f"事件过少无法验证 seqs={seqs}")
else:
    step("A3 事件流单调递增且见 final", False, "无 run 可订阅")

# ── A4 workspace 收敛 ──
r = c.get(f"/agent/conversations/{pid}/workspace")
ws = r.json() if r.status_code == 200 else {}
turn_view = ws.get("active_turn") or {}
step("A4 workspace：会话/当前方案/无悬挂 run",
     r.status_code == 200 and len(ws.get("conversation") or []) >= 2
     and ws.get("active_run") is None
     and turn_view.get("status") in ("answered", "partial", "failed", "cancelled", "running", "needs_input"),
     f"conv={len(ws.get('conversation') or [])} turn={turn_view.get('status')} plan={'有' if ws.get('current_plan') else '无'}")
step("A4 turn 终态不静默（visible_reply 非空）",
     bool((turn_view.get("visible_reply") or "").strip()) or turn_view.get("status") == "running",
     str(turn_view.get("visible_reply"))[:80])

# ── A5 澄清语义（开放澄清结构完整即可，不强制出现）──
clars = ws.get("open_clarifications") or []
step("A5 澄清结构（如有）字段完整",
     all("question" in x and "blocking" in x for x in clars),
     f"open={len(clars)}")

# ── A6 追加回合 → 新 run → 取消 ──
r = c.post(f"/agent/conversations/{pid}/turns",
           json={"message": "再帮我多找一些小众一点的展览，最好人少安静"},
           headers={"Idempotency-Key": str(uuid.uuid4())})
d6 = r.json()
run6 = d6.get("run") or {}
if run6.get("id"):
    rc = c.post(f"/agent/runs/{run6['id']}/cancel")
    ok_cancel = rc.status_code == 200
    # 等待终态（cancel_requested → worker 收敛）
    final_status = None
    for _ in range(60):
        time.sleep(2)
        wsx = c.get(f"/agent/conversations/{pid}/workspace").json()
        act = wsx.get("active_run")
        if act is None or act.get("id") != run6["id"]:
            final_status = "terminal"
            break
    step("A6 cancel 后 run 收敛（不悬挂）", ok_cancel and final_status == "terminal",
         f"cancel HTTP {rc.status_code}")
else:
    step("A6 cancel 后 run 收敛（不悬挂）",
         d6.get("turn_status") in ("answered", "needs_input"),
         f"本轮未产生 run（{d6.get('turn_status')}），取消语义由 R 级用例覆盖")

# ── A7 design_itinerary（v4 语义）──
r = c.post(f"/agent/conversations/{pid}/turns",
           json={"message": "既要去中共一大纪念馆，也要去看微讲座，帮我设计一条路线"},
           headers={"Idempotency-Key": str(uuid.uuid4())})
d7 = r.json()
has_route_or_run = bool(d7.get("route_plan")) or bool(d7.get("run")) \
    or bool((d7.get("assistant_message") or {}).get("content"))
step("A7 design_itinerary：路线卡或 run，不留白", has_route_or_run,
     f"route_plan={'有' if d7.get('route_plan') else '无'} run={'有' if d7.get('run') else '无'} status={d7.get('turn_status')}")
if d7.get("run"):
    read_events(d7["run"]["id"], max_seconds=600)  # 等它收敛，避免脏 run 影响后续

# ── A8 硬 KPI ──
r = c.get("/agent/metrics")
m = r.json() if r.status_code == 200 else {}
step("A8 硬 KPI 全零（silent/promised/hidden）",
     m.get("turn_silent_terminal_total") == 0
     and m.get("promised_without_run_total") == 0
     and m.get("hidden_clarification_total") == 0,
     json.dumps({k: m.get(k) for k in (
         "turn_total", "turn_silent_terminal_total",
         "promised_without_run_total", "hidden_clarification_total")}, ensure_ascii=False))

n_pass = sum(1 for _, ok, _ in results if ok)
print(f"\n==== v4 汇总: PASS {n_pass}/{len(results)} ====")
with open("测试报告/e2e_v4_agent_results.json", "w", encoding="utf-8") as f:
    json.dump([{"name": n, "result": "PASS" if ok else "FAIL", "detail": det}
               for n, ok, det in results], f, ensure_ascii=False, indent=2)
sys.exit(0 if n_pass == len(results) else 1)
