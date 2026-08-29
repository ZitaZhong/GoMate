# -*- coding: utf-8 -*-
"""「周末去哪儿」E2E 验收套件 —— PRD 条款可追溯，输入/预期断言来自 PRD，不来自实现。

每条用例：ID | 条款出处 | 构造输入 | 预期（PRD 推导）| 实际 | PASS/FAIL。
运行：python e2e_acceptance.py [base_url]   默认 http://127.0.0.1:8000
输出：控制台表格 + e2e_results.json；任一 FAIL 退出码 1。
"""
import json, sys, time, urllib.request, urllib.error, io

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SIX = {"confirmed_by_user", "official_source_confirmed", "public_source_observed",
       "estimated", "unknown", "expired"}
TRUSTED = {"official_source_confirmed", "public_source_observed"}
RESULTS = []

# ---------- HTTP ----------
def req(method, path, body=None, timeout=120):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), {}

def sse(method, path, body=None, timeout=300):
    st, raw, _ = req(method, path, body, timeout)
    events, cur = [], {}
    for line in raw.splitlines():
        if line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"] = cur.get("data", "") + line[5:].strip()
        elif line == "" and cur:
            events.append(cur); cur = {}
    if cur:
        events.append(cur)
    out = []
    for e in events:
        try:
            out.append((e["event"], json.loads(e["data"])))
        except Exception:
            out.append((e["event"], {}))
    return st, out

# ---------- 递归扫描 ----------
def walk_evidence(obj, path="$"):
    """产出 (路径, evidence_dict)。"""
    if isinstance(obj, dict):
        if "verification_status" in obj and ("source_type" in obj or "confidence" in obj):
            yield path, obj
        for k, v in obj.items():
            yield from walk_evidence(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_evidence(v, f"{path}[{i}]")

def case(cid, clause, name, given, expect, fn):
    t0 = time.time()
    try:
        passed, actual = fn()
    except Exception as e:
        passed, actual = False, f"执行异常: {type(e).__name__} {e}"
    RESULTS.append({"id": cid, "clause": clause, "name": name, "given": given,
                    "expect": expect, "actual": actual, "passed": bool(passed),
                    "secs": round(time.time() - t0, 1)})
    print(f"[{'PASS' if passed else 'FAIL'}] {cid} {name}  ({RESULTS[-1]['secs']}s)")

# ---------- 公共动作 ----------
def make_plan(constraints, party=None):
    st, raw, _ = req("POST", "/plans", {"constraints": constraints, "party": party or []})
    assert st == 200, f"create plan HTTP {st}: {raw[:200]}"
    return json.loads(raw)["plan_id"]

def stream(pid):
    st, evs = sse("GET", f"/plans/{pid}/stream")
    assert st == 200, f"stream HTTP {st}"
    return evs

def resume(pid, bookings):
    st, evs = sse("POST", f"/plans/{pid}/resume", {"bookings": bookings})
    assert st == 200, f"resume HTTP {st}"
    return evs

def get_event(evs, name):
    return [d for e, d in evs if e == name]

WEEKEND = {"earliest_depart": "2026-07-24T18:00:00+08:00",
           "latest_return": "2026-07-26T22:00:00+08:00"}
HOTEL = {"kind": "hotel", "extracted": {"name": "验收测试酒店"},
         "confirmed": True,
         "evidence": {"source_type": "user_provided", "verification_status": "confirmed_by_user", "confidence": 1.0}}

# ============================================================
# A. 服务契约
# ============================================================
case("A1", "工程契约", "健康检查", "GET /health", "200 且 ok=true",
     lambda: (lambda st, raw, _: (st == 200 and json.loads(raw).get("ok") is True,
                                  f"HTTP {st} {raw[:60]}"))(*req("GET", "/health")))

def _a2():
    r = urllib.request.Request(BASE + "/plans", data=b"not json", method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return False, f"HTTP {resp.status}（应 422）"
    except urllib.error.HTTPError as e:
        return e.code == 422, f"HTTP {e.code}"

case("A2", "工程契约", "非法 JSON 返回 422", "POST /plans body='not json'",
     "422 参数校验错误", _a2)

case("A3", "工程契约/P2", "不存在的计划返回 404", "GET /plans/999999/state", "404",
     lambda: (lambda st, raw, _: (st == 404, f"HTTP {st}"))(*req("GET", "/plans/999999/state")))

# ============================================================
# B. 两段式主闭环（同城，PRD §04/§06）
# ============================================================
_pid = {}
def _b1():
    pid = make_plan({"origins": ["上海"], "target_city_code": "310000",
                     "interests": ["展览", "美食"], "dietary": ["香菜"],
                     "query": "周末 看展 吃本帮菜", **WEEKEND})
    _pid["same"] = pid
    evs = stream(pid)
    kinds = [e for e, _ in evs]
    ok = kinds.count("node_output") >= 4 and "interrupt" in kinds
    _pid["same_evs"] = evs
    return ok, f"事件序列 {kinds}"
case("B1", "PRD §04 两段式", "探索流以 interrupt 暂停等回填",
     "上海同城+完整约束", "≥4 个节点输出后以 interrupt 中断", _b1)

def _b2():
    inter = get_event(_pid["same_evs"], "interrupt")[0]
    eb = inter.get("explore_bundle") or {}
    cities = eb.get("cities") or []
    return len(cities) >= 3, f"候选城市 {len(cities)} 个: {[c.get('name') for c in cities]}"
case("B2", "PRD §04/§5.2", "探索版给出 3 个候选城市",
     "同上", "cities ≥ 3（PRD: 3 个候选目的城市）", _b2)

def _b3():
    inter = get_event(_pid["same_evs"], "interrupt")[0]
    acts = (inter.get("explore_bundle") or {}).get("activities") or []
    if not acts:
        return False, "活动为空"
    bad = [a.get("title") for a in acts
           if a.get("verification_status") not in TRUSTED or not a.get("start_at")]
    return (len(bad) == 0,
            f"活动 {len(acts)} 场；非可信源/缺时间: {bad or '无'}")
case("B3", "PRD §5.3/§06", "活动均为可信源且带时间",
     "同上", "全部活动 verification_status ∈ {官方确认,公开来源} 且有 start_at", _b3)

def _b4():
    inter = get_event(_pid["same_evs"], "interrupt")[0]
    bad = [(p, ev.get("verification_status"))
           for p, ev in walk_evidence(inter) if ev.get("verification_status") not in SIX]
    return len(bad) == 0, f"越界状态 {len(bad)} 处: {bad[:3]}"
case("B4", "PRD §06 六态", "探索版全字段证据状态合法",
     "同上", "递归扫描所有 evidence.verification_status ∈ 六态", _b4)

def _b6():
    evs = resume(_pid["same"], [HOTEL])
    _pid["same_done"] = evs
    kinds = [e for e, _ in evs]
    return "done" in kinds and "interrupt" not in kinds, f"事件序列 {kinds}"
case("B6", "PRD §04 + v2-P0-2", "回填后一次走出确认版（不死循环）",
     "同城+latest_return，回填酒店", "出现 done 且不再回到 interrupt", _b6)

def _b7():
    b = get_event(_pid["same_done"], "done")[0].get("bundle") or {}
    tl = b.get("timeline") or []
    inverted = [s.get("title") for s in tl
                if s.get("start_at") and s.get("end_at") and s["end_at"] <= s["start_at"]]
    return (len(tl) >= 3 and not inverted and (b.get("validation") or {}).get("ok") is True,
            f"槽位 {len(tl)}；倒序时间段 {inverted or '无'}；validation.ok={(b.get('validation') or {}).get('ok')}")
case("B7", "PRD §06/§5.12", "时间线完整且无倒序时间段",
     "确认版 bundle", "≥3 槽位；所有段 end>start；validation.ok=true", _b7)

def _b8():
    b = get_event(_pid["same_done"], "done")[0].get("bundle") or {}
    bad = [p for p, ev in walk_evidence(b)
           if ev.get("verification_status") == "confirmed_by_user"
           and ev.get("source_type") != "user_provided"]
    return len(bad) == 0, f"违规 {len(bad)} 处: {bad[:3]}"
case("B8", "PRD §11 诚实 KPI", "confirmed_by_user 只能来自 user_provided",
     "确认版 bundle", "递归扫描：未确认误展为已确认 = 0", _b8)

def _b9():
    b = get_event(_pid["same_done"], "done")[0].get("bundle") or {}
    dining = b.get("dining") or []
    fb = [d for d in dining if d.get("is_fallback")]
    return len(dining) >= 1 and len(fb) >= 1, f"餐饮 {len(dining)} 家，稳妥备选 {len(fb)} 家"
case("B9", "PRD §5.7", "餐饮恒有稳妥备选",
     "确认版 bundle", "dining ≥1 且至少 1 家 is_fallback", _b9)

def _b10():
    st, raw, _ = req("GET", f"/plans/{_pid['same']}/calendar.ics")
    ok = (st == 200 and "BEGIN:VCALENDAR" in raw
          and "TZID=Asia/Shanghai" in raw and "DTSTART;TZID=Asia/Shanghai" in raw)
    z_bad = "DTSTART:" in raw and "Z" in [l for l in raw.splitlines() if l.startswith("DTSTART")][0][:40]
    return ok and not z_bad, f"HTTP {st}；含上海时区={'TZID=Asia/Shanghai' in raw}；UTC错标={z_bad}"
case("B10", "PRD §08 + v2-P1-4", "ICS 导出且时区正确",
     "GET calendar.ics", "200；VEVENT 带 TZID=Asia/Shanghai 本地时间，不错标 UTC", _b10)

# ============================================================
# C. 跨城交通（PRD §5.4/§5.5）
# ============================================================
def _c1():
    pid = make_plan({"origins": ["上海"], "target_city_code": "320100",
                     "interests": ["美食"], "query": "周末 南京 逛吃",
                     "weekend_start": "2026-07-25", **WEEKEND})
    _pid["cross"] = pid
    evs = stream(pid)
    _pid["cross_evs"] = evs
    t = next(d for e, d in evs if "transport_options" in d)["transport_options"]
    c = (t.get("candidates") or [{}])[0]
    ok = c.get("recommended_mode") not in (None, "local")
    return ok, f"mode={c.get('recommended_mode')}（v2 曾恒 local）"
case("C1", "PRD §5.4 + v2-P0-1", "上海出发跨城不判同城",
     "上海→南京", "recommended_mode ≠ local", _c1)

def _c2():
    t = next(d for e, d in _pid["cross_evs"] if "transport_options" in d)["transport_options"]
    c = (t.get("candidates") or [{}])[0]
    dd = c.get("door_to_door") or {}
    rail = (dd.get("rail") or {}).get("total_min") or 0
    air = (dd.get("air") or {}).get("total_min") or 0
    return rail > 0 and air > 0, f"rail={rail}min air={air}min（v2 曾全 0）"
case("C2", "PRD §5.5", "门到门双模式有真实估算",
     "上海→南京", "rail/air total_min 均 > 0", _c2)

def _c3():
    pid = make_plan({"origins": ["上海"], "target_city_code": "110000",
                     "interests": ["展览"], "query": "周末 北京 看展", **WEEKEND})
    evs = stream(pid)
    t = next(d for e, d in evs if "transport_options" in d)["transport_options"]
    c = (t.get("candidates") or [{}])[0]
    dd = c.get("door_to_door") or {}
    rail = (dd.get("rail") or {}).get("total_min") or 0
    air = (dd.get("air") or {}).get("total_min") or 0
    return 0 < air < rail, f"1200km：air={air}min < rail={rail}min（门到门飞机应更快）"
case("C3", "PRD §5.5 门到门逻辑", "长途门到门飞机快于高铁",
     "上海→北京(~1200km)", "0 < air < rail", _c3)

def _c4():
    inter = get_event(_pid["cross_evs"], "interrupt")[0]
    pre = inter.get("prefill") or {}
    ok = (pre.get("rail") or {}).get("from") == "上海" and (pre.get("rail") or {}).get("to") == "南京"
    return ok, f"prefill={json.dumps(pre, ensure_ascii=False)}"
case("C4", "PRD §5.4-C/E", "回填预填清单带发到站",
     "上海→南京 interrupt", "prefill.rail = {from:上海, to:南京}", _c4)

def _b5():
    inter = get_event(_pid["cross_evs"], "interrupt")[0]
    t_dict = (inter.get("explore_bundle") or {}).get("transport") or {}
    t = json.dumps(t_dict, ensure_ascii=False)
    has_price_key = '"price"' in t or '"fare"' in t or '"ticket_price"' in t
    is_local = (t_dict.get("candidates") or [{}])[0].get("recommended_mode") == "local"
    disclaimer_ok = is_local or "为准" in t  # 同城无城际票价可声明；城际必须有
    return (not has_price_key and disclaimer_ok,
            f"含价格键={has_price_key}；同城={is_local}；含'以官方为准'声明={'为准' in t}")
case("B5", "PRD §5.4 禁编", "跨城交通策略不含票价/余票数字",
     "上海→南京 探索版", "无 price/fare 字段，且有'以官方平台为准'声明", _b5)

# ============================================================
# D. 回填抽取（PRD 原则三/§5.4-E）
# ============================================================
def _d1():
    st, raw, _ = req("POST", f"/plans/{_pid['cross']}/bookings/import",
                     {"kind": "train", "input_kind": "text",
                      "raw": "【铁路12306】您已购7月24日D952次 上海虹桥-南京南 18:44开 二等座 票价115元", "extracted": {}})
    d = json.loads(raw)
    ex = (d.get("booking") or {}).get("extracted") or {}
    ok = bool(ex.get("train_no") and ex.get("from_station") and ex.get("to_station"))
    return ok, f"HTTP {st}；抽取={json.dumps(ex, ensure_ascii=False)}"
case("D1", "PRD 原则三", "粘贴订单文本自动结构化",
     "D952 次新格式文本", "抽出车次/发到站", _d1)

def _d2():
    st, raw, _ = req("POST", f"/plans/{_pid['cross']}/bookings/import",
                     {"kind": "train", "input_kind": "text",
                      "raw": "G17次 上海虹桥-南京南 19:02开", "extracted": {}})
    d = json.loads(raw)
    ev = ((d.get("booking") or {}).get("evidence") or {})
    vs = ev.get("verification_status")
    return vs != "confirmed_by_user", f"状态={vs}（用户未确认却被标'用户已确认'）"
case("D2", "PRD 原则三/§11", "机器抽取初稿不得直接标 confirmed_by_user",
     "仅粘贴文本、无人工确认动作", "初稿状态 ≠ confirmed_by_user（须经用户确认）", _d2)

# ============================================================
# E. Copilot 对话（DD-15，用户真实话术）
# ============================================================
def _e1():
    pid = _pid["cross"]
    st, raw, _ = req("POST", f"/plans/{pid}/chat",
                     {"message": "我们仨下周六从苏州出发去南京玩两天，主要想吃鸭子逛博物院，人均别超八百"})
    d = json.loads(raw)
    patch = d.get("constraints_patch") or {}
    ok = (d.get("intent") == "provide_constraints" and d.get("reply")
          and "苏州" in json.dumps(patch, ensure_ascii=False))
    return ok, f"intent={d.get('intent')} patch={json.dumps(patch, ensure_ascii=False)} reply={(d.get('reply') or '')[:40]}"
case("E1", "DD-15", "一句话约束被抽取并回复人话",
     "苏州/南京/吃鸭子/博物院/八百", "intent=provide_constraints；patch 含苏州；reply 非空调试文本", _e1)

def _e2():
    st, raw, _ = req("POST", f"/plans/{_pid['cross']}/chat", {"message": "目的地换成无锡吧"})
    d = json.loads(raw)
    patch = json.dumps(d.get("constraints_patch") or {}, ensure_ascii=False)
    ok = "无锡" in patch and "origins" not in patch
    return ok, f"patch={patch}（'去无锡'被当成'从无锡出发'即错）"
case("E2", "DD-15 语义", "'目的地换X'不得改出发地",
     "目的地换成无锡吧", "patch 改目的地字段，不得写 origins", _e2)

def _e3():
    st, raw, _ = req("POST", f"/plans/{_pid['cross']}/chat",
                     {"message": "房间我订好了，桔子水晶南京新街口店，周五入住周日退房"})
    d = json.loads(raw)
    return d.get("intent") == "confirm_booking", f"intent={d.get('intent')}（回填被当闲聊即丢数据）"
case("E3", "DD-15 意图", "'订好了'触发回填意图",
     "房间我订好了…", "intent=confirm_booking", _e3)

def _e4():
    pid = make_plan({"origins": ["上海"], "target_city_code": "310000",
                     "interests": ["展览"], "query": "周末 莫奈", **WEEKEND})
    stream(pid)
    st, raw, _ = req("POST", f"/plans/{pid}/chat", {"message": "莫奈那个展在哪儿办？"})
    d = json.loads(raw)
    reply = d.get("reply") or ""
    return "中华艺术宫" in reply, f"reply={reply[:60]}（库内有该展却答不出=检索未命中）"
case("E4", "DD-15/§5.3", "问答命中库内已有活动",
     "莫奈那个展在哪儿办？", "reply 提及'中华艺术宫'（库内确有此展）", _e4)

def _e5():
    st, raw, _ = req("POST", f"/plans/{_pid['cross']}/chat", {"message": "zxcvbnm"})
    d = json.loads(raw)
    ok = st == 200 and d.get("intent") in {"provide_constraints", "clarify_answer", "refine_field",
                                           "deep_research", "confirm_booking", "ask_info", "chitchat"}
    return ok, f"HTTP {st} intent={d.get('intent')}"
case("E5", "DD-15 韧性", "无意义输入不崩溃且有兜底意图",
     "zxcvbnm", "200 且意图合法", _e5)

# ============================================================
# F. 多人协作（PRD §5.1）
# ============================================================
def _f_setup():
    pid = make_plan({"origins": ["上海"], "target_city_code": "310000",
                     "interests": ["展览"], "query": "周末", **WEEKEND})
    _pid["party"] = pid
    return pid

def _f1():
    pid = _f_setup()
    st1, r1, _ = req("POST", f"/plans/{pid}/invites", {})
    st2, r2, _ = req("POST", f"/plans/{pid}/invites", {})
    d1, d2 = json.loads(r1), json.loads(r2)
    t1 = d1["invites"][0]["token"]; t2 = d2["invites"][0]["token"]
    l1 = d1["invites"][0]["anon_label"]; l2 = d2["invites"][0]["anon_label"]
    _pid["tokens"] = (t1, t2)
    ok = st1 == 200 and t1 != t2 and l1 != l2
    return ok, f"token 各异={t1 != t2}；标签 {l1!r}/{l2!r}（同名则无法区分同伴）"
case("F1", "PRD §5.1", "邀请链接生成且同伴可区分",
     "连发 2 个邀请", "token 不同、匿名标签不同", _f1)

def _f2():
    t1, t2 = _pid["tokens"]
    req("POST", f"/invite/{t1}/constraints",
        {"origins": ["杭州"], "earliest_depart": "2026-07-24T16:00:00+08:00",
         "latest_return": "2026-07-26T23:00:00+08:00",
         "budget_band": {"min": 300, "max": 1200}, "interests": ["展览"], "dietary": ["葱"], "accept_flight": True})
    req("POST", f"/invite/{t2}/constraints",
        {"origins": ["南京"], "earliest_depart": "2026-07-24T19:30:00+08:00",
         "latest_return": "2026-07-26T20:00:00+08:00",
         "budget_band": {"min": 600, "max": 2000}, "interests": ["美食"], "dietary": ["蒜"], "accept_flight": False})
    st, raw, _ = req("GET", f"/plans/{_pid['party']}/party/aggregate")
    agg = json.loads(raw).get("aggregated") or {}
    exp = {"earliest_depart": "2026-07-24T19:30:00+08:00",  # max
           "latest_return": "2026-07-26T20:00:00+08:00",    # min
           "budget": (600, 1200), "flight": False, "size": 2,
           "interests": {"展览", "美食"}, "dietary": {"葱", "蒜"}}
    got = {"earliest_depart": agg.get("earliest_depart"), "latest_return": agg.get("latest_return"),
           "budget": ((agg.get("budget_band") or {}).get("min"), (agg.get("budget_band") or {}).get("max")),
           "flight": agg.get("accept_flight"), "size": agg.get("party_size"),
           "interests": set(agg.get("interests") or []), "dietary": set(agg.get("dietary") or [])}
    ok = (got["earliest_depart"].startswith("2026-07-24T11:30")  # 19:30+08:00=11:30Z
          and got["latest_return"].startswith("2026-07-26T12:00")
          and got["budget"] == (600, 1200) and got["flight"] is False and got["size"] == 2
          and got["interests"] == exp["interests"] and got["dietary"] == exp["dietary"])
    return ok, json.dumps(got, ensure_ascii=False, default=str)
case("F2", "PRD §5.1 聚合语义", "聚合=出发取max/返程取min/预算交集/兴趣并集",
     "杭州(16:00-23:00,300-1200,展览,葱,可飞) + 南京(19:30-20:00,600-2000,美食,蒜,不飞)",
     "19:30/20:00/[600,1200]/不飞/2人/{展览,美食}/{葱,蒜}", _f2)

def _f3():
    st, raw, _ = req("GET", f"/plans/{_pid['party']}/party/aggregate")
    text = raw
    leaks = [s for s in ('"300"', '"2000"', "杭州", "南京") if s in text.replace('"min": 300', "").replace('"max": 2000', "")]
    # 个体预算极值 300/2000 与个体出发地不应出现在聚合视图（交集/包络值 600/1200 除外）
    return len(leaks) == 0, f"泄漏项 {leaks or '无'}"
case("F3", "PRD §5.1 隐私", "聚合视图不暴露个体预算/出发地",
     "GET party/aggregate", "不含成员个体预算极值与城市名", _f3)

# ============================================================
# G. 韧性与诚实（PRD §09/§11）
# ============================================================
def _g1():
    pid = make_plan({"origins": ["上海"], "target_city_code": "120000",
                     "interests": ["展览"], "query": "周末 天津 看展", **WEEKEND})
    evs = stream(pid)
    inter = get_event(evs, "interrupt")[0]
    eb = inter.get("explore_bundle") or {}
    warns = eb.get("warnings") or eb.get("warnings", [])
    research = next((d for e, d in evs if "research" in d), {}).get("research") or {}
    has_sources = bool(research.get("official_sources"))
    has_hint = any("官方" in w or "链接" in w for w in warns)
    return ((eb.get("activities") == [] and (has_sources or has_hint)),
            f"活动空={eb.get('activities') == []}；官方源 {len(research.get('official_sources') or [])} 个；提示={warns}")
case("G1", "PRD §09 韧性", "无数据城市：给官方源清单而非编造活动",
     "上海→天津(库内 0 活动)", "activities=[] 且附官方源/链接提示", _g1)

def _g2():
    pid = make_plan({})
    evs = stream(pid)
    kinds = [e for e, _ in evs]
    return "interrupt" in kinds, f"事件序列 {kinds}"
case("G2", "PRD §09 韧性", "空约束也能出探索版（默认兜底）",
     "POST /plans {} 直接 stream", "不 5xx，产出 interrupt", _g2)

def _g3():
    inter = get_event(_pid["same_evs"], "interrupt")[0]
    acts = (inter.get("explore_bundle") or {}).get("activities") or []
    bad = [a.get("title") for a in acts
           if a.get("start_at") and a.get("end_at") and a["end_at"] <= a["start_at"]]
    return len(bad) == 0, f"倒序/过期活动 {len(bad)} 场: {bad[:2]}"
case("G3", "PRD §11 可信度", "入库活动时间段合法（无倒序）",
     "探索版活动全量", "全部 end_at > start_at", _g3)

def _g4():
    b = get_event(_pid["same_done"], "done")[0].get("bundle") or {}
    exp = {"todo_checklist": ["待确认", "checklist", "todo"], "hotel_area": ["hotel", "lodg", "住宿"]}
    missing = [k for k, keys in exp.items()
               if k not in b and not any(any(t in kk for t in keys) for kk in b.keys())]
    return len(missing) == 0, f"探索/确认版缺字段: {missing}（PRD §06 要求）"
case("G4", "PRD §06", "Bundle 含待确认清单与住宿区域",
     "确认版 bundle keys", "含 待确认清单 + 住宿区域", _g4)

# ============================================================
# 汇总
# ============================================================
passed = sum(1 for r in RESULTS if r["passed"])
total = len(RESULTS)
print(f"\n===== 结果 {passed}/{total} 通过 =====")
for r in RESULTS:
    mark = "PASS" if r["passed"] else "FAIL"
    print(f"[{mark}] {r['id']:<4} {r['clause']:<14} {r['name']}")
    if not r["passed"]:
        print(f"       输入: {r['given']}")
        print(f"       预期: {r['expect']}")
        print(f"       实际: {r['actual']}")

with io.open("e2e_results.json", "w", encoding="utf-8") as f:
    json.dump({"base": BASE, "passed": passed, "total": total, "cases": RESULTS},
              f, ensure_ascii=False, indent=1)
sys.exit(0 if passed == total else 1)
