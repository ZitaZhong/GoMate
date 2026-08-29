# 下周末活动时效自验证：chat 建 plan → SSE 到探索版 → 校验活动全部覆盖目标周末
# 判定标准：
#   1. 每条活动 start_at <= weekend_end 且 coalesce(end_at, start_at) >= weekend_start（无一过期）
#   2. 城市卡"当周 N 场"计数 == bundle activities 列表长度（口径一致）
#   3. reason/文案无"本周末"（应为"当周"）
import json
import sys
from datetime import datetime

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
fails = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('| ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


c = httpx.Client(base_url=BASE, timeout=180)

# 1. chat-first 建 plan（下周末）
r = c.post("/plans/new/chat", json={"message": "下周末从上海去杭州，喜欢看展"})
d = r.json()
pid = d.get("plan_id")
check("chat 建 plan", bool(pid), f"pid={pid}")
ws = d["constraints"].get("weekend_start")
we = d["constraints"].get("weekend_end")
print(f"  weekend: {ws} ~ {we}")
wk_s = datetime.fromisoformat(ws)
wk_e = datetime.fromisoformat(we)

# 2. SSE 到 interrupt/done
if d.get("restart_stream"):
    last_ev = None
    with c.stream("GET", f"/plans/{pid}/stream", timeout=600) as resp:
        for line in resp.iter_lines():
            if line.startswith("event:"):
                last_ev = line[6:].strip()
            if last_ev in ("interrupt", "done"):
                break
    print("  stream ended at:", last_ev)

# 3. 取 bundle 校验
b = c.get(f"/plans/{pid}/bundle").json()
eb = b.get("explore") or {}
acts = eb.get("activities") or []
check("bundle 有活动", len(acts) > 0, f"{len(acts)} 场")

stale = []
undated = 0
for a in acts:
    # codex 开放语义：非日期型候选（常年开放的街区/场馆）start_at 可为空——
    # 不参与过期判定（无日期即无“过期”概念），但计入口径统计。
    if not a.get("start_at"):
        undated += 1
        continue
    s = datetime.fromisoformat(a["start_at"].replace("Z", "+00:00"))
    e = datetime.fromisoformat(a["end_at"].replace("Z", "+00:00")) if a.get("end_at") else s
    if not (s <= wk_e and e >= wk_s):
        stale.append((a["title"][:20], a["start_at"], a.get("end_at")))
check("有日期活动全部覆盖目标周末（无一过期）", not stale,
      f"违规 {stale}（无日期候选 {undated} 条不参与判定）")

# 4. 计数口径一致
cities = eb.get("cities") or []
if cities:
    n_card = (cities[0].get("driven_by_activities") or {}).get("value")
    check("卡计数 == 列表长度", n_card == len(acts), f"卡 {n_card} vs 列表 {len(acts)}")
    reason = cities[0].get("reason") or ""
    check("卡文案用『当周』", "当周" in reason and "本周末" not in reason, reason[:40])

print("\n==== 汇总 ====")
print("PASS ALL" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
