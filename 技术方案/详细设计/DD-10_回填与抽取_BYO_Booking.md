# DD-10 回填与抽取（BYO Booking）· 详细设计

**详细设计系列 · 领域模块文档 · v1.0 · 2026 年 7 月**

> 本文定义「周末去哪儿」的**回填与抽取管道**（Bring Your Own Booking）：用户在官方平台买好票/订好房后，把凭证以**文本/截图/链接**三种入口回填，经**统一抽取管道**结构化，再经**用户逐字段确认**写入 `bookings` 表，最终经 DD-02 `await_booking` 的 `Command(resume)` 注入编排状态机。这是 TripIt "订单即数据"原则的现代 LLM 版本，但用**强制逐字段确认**替代 TripIt 的"自动导入"，贯彻产品硬约束"证据优先"。
>
> **上游依据**：DD-01（`bookings` 表、`booking_kind` 枚举、OSS 截图布局与 7 天生命周期 §9.2、隐私 §11）、DD-02（`await_booking` 中断/`resume` 契约 §6.2）、DD-03（`confirmed_by_user`、`user_provided` 来源、`enforce_provenance`）、DD-04（Qwen-VL-OCR、小模型抽取、PydanticAI `output_type`、`redact()`、`ResilientProvider` 禁裸调）、技术架构 v1（§9.3 回填三入口）、PRD 对外版（原则三、07 浏览器扩展隐私）、竞品 §5.4（TripIt 解析引擎、统一 Master Itinerary）。
> **下游消费者**：DD-02（resume 注入 `bookings`）、DD-12（时间线消费 `bookings`）、DD-13（确认版 bundle 花费）、DD-14（浏览器扩展复用本管道）。
> **一句话**：**抽取只是初稿，确认才是事实。未经用户逐字段确认的抽取结果，永不入库为 `confirmed`，永不进时间线。**

---

## 1. 模块职责与边界

| 项 | 说明 |
|---|---|
| **职责** | 提供"回填三入口 → 统一抽取 → 逐字段确认 → 写 `bookings`(confirmed) → 供 DD-02 resume 注入"的完整闭环；准备 Master Timeline 合并结构（供 DD-12）。 |
| **边界内** | 三入口抽取（文本/截图/链接）、抽取 schema（train/flight/hotel）、OCR 与链接抓取调度、逐字段确认接口、写库、PII 打码、截图生命周期对接、resume payload 组装。 |
| **边界外** | ① 编排流转与 checkpoint（DD-02 负责 `interrupt/resume`，本模块只提供确认后的数据）；② OCR/LLM 原子能力（DD-04 提供，本模块只调 `extract_fact`，**不得裸调**）；③ 证据定级规则（DD-03 提供 `enforce_provenance`/`FIELD_SOURCE_POLICY`）；④ 时间线求解（DD-12）；⑤ 交通策略与预填提示的**生成**（DD-09 产 `prefill`，本模块只做"预填 vs 回填"对照）。 |
| **架构位置** | v1 §4.1 分层图"AI 能力层·抽取"与 BFF 之间的领域服务；回填发生在 DD-02 `await_booking` **中断期**，由 **BFF 调用本模块**，确认后经 `resume` 恢复编排。 |

**在编排中的位置（DD-02 §6 时序）**：

```text
transport 产出探索版 → await_booking: interrupt() 持久化 → BFF 收到中断
   ↓ 前端展示探索版 + 预填清单(prefill) + 起售提醒
[用户离开去 12306 / 航司 / 酒店平台买票……几小时~几天]
用户回填(本模块: 抽取) → 逐字段确认(本模块) → 写 bookings(confirmed)
   ↓ BFF: POST /plans/{id}/resume
graph.invoke(Command(resume=confirmed_bookings)) → hotel..compose 续跑
```

**设计原则**：① **抽取不可信、确认才可信**——抽取准确率不足由确认兜底，绝不以抽取直接入时间线；② **三入口一条管道**——文本/截图/链接归一到同一 `Fact` 结构与同一确认闭环，扩展（DD-14）亦复用；③ **轻后端重证据**——不落 Key 明文、截图 7 天即删、PII 打码，证据随字段走。

---

## 2. 设计目标与非目标

**目标**：
1. **三入口统一**：文本（小模型）、截图（Qwen-VL-OCR + OSS）、链接（抓取公开页 → 小模型），产出同构 `list[BookingFact]`。
2. **确认闭环无脏数据**：任何字段未经用户确认，`bookings.confirmed=False`；`resume` 只注入 `confirmed=True` 记录；DD-12 只读 `confirmed=True`。
3. **极致顺滑（对标 TripIt）**：抽取一次成型的字段预勾选，用户只需改错项；截图入口从"手输一分钟"降到"截图+确认十秒"。
4. **防幻觉**：`train.*`/`flight.*` 关键字段来源恒为 `user_provided`（经确认），杜绝 LLM 编造票价/余票（DD-03 闸三）。
5. **隐私合规**：证件号/PII 打码后落库；截图 OSS 7 天生命周期 + 签名 URL；进模型前 `redact()`。

**非目标**：
- ❌ 不做交易/代购/模拟登录（PRD 原则）——只接收用户已完成的预订。
- ❌ 不追求抽取 100% 准确率——确认闭环是兜底，抽取只降低用户输入量。
- ❌ 不做邮件自动导入（TripIt 模式）——v0.1 只做用户主动回填三入口；邮件/PDF 摄取延后。
- ❌ 不生成 `train.*`/`flight.*` 事实值（禁编，回官方/用户）。

---

## 3. 数据模型

### 3.1 `bookings` 表（引用 DD-01 §8.2，本模块为唯一写入方）

DD-01 已定义，此处**不重复 DDL，只标注本模块的字段语义与写入规则**：

| 字段 | 类型 | 本模块写入语义 |
|---|---|---|
| `plan_id` | BIGINT | 归属计划；中断期由 BFF 从 thread 上下文带入 |
| `kind` | `booking_kind` = `train`/`flight`/`hotel` | 抽取器路由与 schema 选择依据 |
| `raw_input` | TEXT | text：原始文本；image：**OSS 对象键**`screenshots/{plan_id}/{uuid}.jpg`；link：URL；manual：空 |
| `input_kind` | TEXT = `text`/`image`/`link`/`manual` | 入口类型；决定抽取路径与降级策略 |
| `extracted` | JSONB | 抽取草稿（§3.2 schema），含每字段的 `evidence_quote`/`confidence`；**确认前也落库**（便于跨天恢复编辑草稿） |
| `evidence` | JSONB (NOT NULL) | DD-01 §5 标准结构；**未确认**=`{source_type:'user_provided', verification_status:'unknown', note:'needs_review'}`；**确认后**=`verification_status:'confirmed_by_user'` |
| `confirmed` | BOOLEAN | 逐字段确认全部通过后置 `True`；**唯一入时间线闸门** |
| `confirmed_at` | TIMESTAMPTZ | 确认时刻 |

> **状态机（本模块内）**：`ingesting`（抽取中）→ `needs_review`（有草稿待确认，`confirmed=False`）→ `confirmed_by_user`（`confirmed=True`）。抽取失败→仍建行，`extracted={}`，走手工输入降级（§8）。状态不新建列，由 `confirmed` + `evidence.verification_status` 联合表达（省成本，对齐 DD-01 "证据随字段走"）。

### 3.2 三类抽取 schema（PydanticAI `output_type` 承载）

字段命名严格对齐需求与 DD-03 `FIELD_SOURCE_POLICY`（`train.train_no`/`train.departure_time`/`train.price`、`flight.flight_no`/`flight.dep_time`/`flight.price`）。每个可对外事实字段用 DD-03 `Fact[T]` 包装（值 + evidence），另附 `evidence_quote`（抽取原文片段，供确认页高亮）。

```python
# dd10/schema.py —— 抽取 schema（PydanticAI output_type；字段即 DD-03 Fact）
from datetime import date, time
from pydantic import BaseModel, Field
from dd03.evidence import Fact           # DD-03 §3 Fact/Evidence

class ExtractField(BaseModel):
    """单字段抽取产物：值 + 抽取原文片段 + 置信度。转 Fact 时 source_type=user_provided。"""
    value: str | None = None
    evidence_quote: str | None = Field(None, description="抽取所依据的原文片段，供确认页高亮")
    confidence: float = 0.0               # 0~1；<阈值前端标黄提示重点核对

class TrainExtract(BaseModel):
    """火车票（12306）。字段前缀映射 DD-03 FIELD_SOURCE_POLICY 'train.*'。"""
    train_no:   ExtractField              # 车次 G1234 → train.train_no
    from_station: ExtractField            # 出发站（含"上海虹桥"这类站名）
    to_station:   ExtractField            # 到达站
    date:       ExtractField              # 乘车日期 YYYY-MM-DD
    depart:     ExtractField              # 开车时刻 HH:MM → train.departure_time
    arrive:     ExtractField              # 到达时刻 HH:MM
    seat_class: ExtractField              # 席别：二等座/一等座/商务座/硬卧...
    price:      ExtractField              # 票价（元）→ train.price（禁 LLM 编，仅用户回填）
    passenger_masked: ExtractField | None = None   # 乘车人：抽取即打码（§9）

class FlightExtract(BaseModel):
    """机票。字段前缀映射 DD-03 FIELD_SOURCE_POLICY 'flight.*'。"""
    flight_no:   ExtractField             # 航班号 MU5100 → flight.flight_no
    dep_airport: ExtractField             # 出发机场/航站楼 SHA·T2
    arr_airport: ExtractField             # 到达机场/航站楼
    date:        ExtractField             # 日期 YYYY-MM-DD
    dep_time:    ExtractField             # 起飞 HH:MM → flight.dep_time
    arr_time:    ExtractField             # 到达 HH:MM
    cabin:       ExtractField             # 舱位：经济舱/公务舱...
    baggage:     ExtractField             # 行李额：20kg / 1件23kg
    price:       ExtractField             # 票价（元）→ flight.price（禁编）

class HotelExtract(BaseModel):
    """酒店确认单。"""
    hotel:        ExtractField            # 酒店名
    area:         ExtractField            # 商圈/区域（脱敏到商圈级，对齐 DD-01 origin_area 粒度）
    check_in:     ExtractField            # 入住日 YYYY-MM-DD
    check_out:    ExtractField            # 离店日 YYYY-MM-DD
    room:         ExtractField            # 房型 大床房/双床房
    cancel_policy:ExtractField            # 取消政策原文（供 DD-13 生成 hotel_cancel_deadline 提醒）
    price:        ExtractField            # 总价（元）

# 路由表：kind → schema + LLM 任务（DD-04 LLM_ROUTES）
SCHEMA_BY_KIND = {"train": TrainExtract, "flight": FlightExtract, "hotel": HotelExtract}
```

> **金额约定**：抽取原文保留（如 "¥553.5"）；写库归一为**分**（`INT`，对齐 DD-01 "金额单位分避免浮点"）。确认页展示元。

### 3.3 `extracted` JSONB 落库形态

```jsonc
// bookings.extracted —— 抽取草稿（确认前后均落库；确认后各字段 evidence 升级）
{
  "schema_version": "dd10-v1",
  "kind": "train",
  "fields": {
    "train_no":   {"value": "G7016", "evidence_quote": "G7016", "confidence": 0.98,
                   "evidence": {"source_type":"user_provided","verification_status":"confirmed_by_user"}},
    "from_station":{"value": "上海虹桥", "evidence_quote":"上海虹桥站", "confidence": 0.95, "evidence": {...}},
    "to_station": {"value": "苏州", "confidence": 0.93, "evidence": {...}},
    "date":       {"value": "2026-07-25", "confidence": 0.9, "evidence": {...}},
    "depart":     {"value": "08:12", "confidence": 0.97, "evidence": {...}},
    "arrive":     {"value": "08:35", "confidence": 0.97, "evidence": {...}},
    "seat_class": {"value": "二等座", "confidence": 0.88, "evidence": {...}},
    "price":      {"value": 3950, "unit":"cent", "confidence": 0.85, "evidence": {...}}  // 39.5 元
  },
  "review": {"all_confirmed": true, "edited_fields": ["seat_class"], "confirmed_at":"2026-07-21T10:20:00Z"}
}
```

---

## 4. 接口契约

本模块以**领域服务**形式暴露给 BFF（中断期由 BFF 编排调用）。所有对外事实抽取经 DD-04 `extract_fact` + DD-03 三闸，**不得裸调 LLM/OCR**。

### 4.1 `ingest_booking(kind, raw)` —— 三入口统一抽取入口

```python
# dd10/service.py
from typing import Literal

async def ingest_booking(
    plan_id: int,
    kind: Literal["train", "flight", "hotel"],
    input_kind: Literal["text", "image", "link", "manual"],
    raw: str,                       # text=文本；image=OSS对象键；link=URL；manual=""
) -> dict:
    """
    统一抽取入口。产出草稿并落库（confirmed=False, evidence.status=unknown/needs_review）。
    返回 draft，供前端渲染逐字段确认页。绝不直接 confirmed，绝不进时间线。
    """
    # 返回结构
    return {
        "booking_id": 123,
        "kind": kind,
        "input_kind": input_kind,
        "draft": { /* §3.3 extracted.fields，各字段带 value/confidence/evidence_quote */ },
        "screenshot_signed_url": "https://oss/...signed",   # image 入口才有，短时效
        "low_confidence_fields": ["seat_class", "price"],   # confidence<阈值，前端标黄
        "prefill_match": { /* §6 与 DD-09 预填对照结果 */ },
        "status": "needs_review",
    }
```

**行为**：
1. 建 `bookings` 行（`confirmed=False`，`evidence.verification_status='unknown'`, `note='needs_review'`）。
2. 按 `input_kind` 走对应抽取路径（§5）。
3. 抽取结果写 `extracted`，返回 draft。**任何异常 → 返回空 draft + `degraded=True`，走手工输入降级（§8），不抛错给用户**。

### 4.2 `confirm_booking(...)` —— 逐字段确认闭环

```python
async def confirm_booking(
    plan_id: int,
    booking_id: int,
    confirmed_fields: dict,         # {字段名: 用户最终确认值}（含用户改过的）
    partial: bool = False,          # 是否允许仅确认部分（未确认字段留 needs_review）
) -> dict:
    """
    用户逐字段确认（可编辑）。全部关键字段确认后：
      - 归一/校验（日期、时刻、金额→分）
      - 各字段 evidence 经 DD-03 enforce_provenance('train.price', value, 'user_provided')
        → verification_status = confirmed_by_user
      - bookings.confirmed=True, confirmed_at=now(), evidence 升级
    返回可供 DD-02 resume 注入的 booking 结构（§5.5）。
    """
    return {
        "booking_id": booking_id,
        "kind": "train",
        "extracted": { /* 确认后的最终值 */ },
        "confirmed": True,
        "evidence": {"source_type": "user_provided",
                     "verification_status": "confirmed_by_user",
                     "confidence": 1.0, "note": "用户逐字段确认"},
        "ready_for_resume": True,
    }
```

**关键字段清单**（必须逐一确认，缺一不置 `confirmed=True`）：
- train：`train_no, from_station, to_station, date, depart`（`arrive/seat_class/price` 建议确认）
- flight：`flight_no, dep_airport, arr_airport, date, dep_time`
- hotel：`hotel, check_in, check_out`（`area/room/cancel_policy/price` 建议确认）

### 4.3 抽取 tool 定义（PydanticAI，供 DD-04 `extract_fact` 调用）

```python
# dd10/extract.py —— 三入口抽取器，全部走 DD-04，禁裸调
from dd04.ai import extract_fact          # DD-04 §6.4：PydanticAI output_type=schema
from dd10.schema import SCHEMA_BY_KIND

TASK_BY_INPUT = {                          # DD-04 §6.1 LLM_ROUTES
    "text":  "activity_extract",           # qwen-turbo 小模型
    "link":  "search_entry",               # 抓正文后小模型抽取
    "image": "booking_ocr",                # qwen-vl-ocr 多模态
}

async def run_extract(kind: str, input_kind: str, payload) -> list:
    schema = SCHEMA_BY_KIND[kind]
    task   = TASK_BY_INPUT[input_kind]
    # source_type 固定 user_provided：这是用户提供的凭证，非系统检索
    return await extract_fact(task=task, text_or_image=payload,
                              schema=schema, source_type="user_provided")
```

### 4.4 与 BFF / DD-02 `await_booking` resume 的对接

| 步骤 | 调用方 | 接口 | 说明 |
|---|---|---|---|
| 1. 上传截图 | BFF | `POST /uploads`（BFF 直传 OSS，返回对象键 + 签名 URL） | 键=`screenshots/{plan_id}/{uuid}.jpg`，7天生命周期 |
| 2. 抽取 | BFF → 本模块 | `ingest_booking(kind, input_kind, raw)` | 返回 draft |
| 3. 确认 | BFF → 本模块 | `confirm_booking(...)` | 返回 `ready_for_resume` 结构 |
| 4. 恢复编排 | BFF → DD-02 | `POST /plans/{id}/resume`，body 见下 | `Command(resume=...)` |

**resume body（严格对齐 DD-02 §6.2）**：

```jsonc
// BFF 汇集本 plan 全部 confirmed bookings 后，一次性 resume 注入
{ "resume": [
    {"kind":"train","extracted":{...},"confirmed":true,
     "evidence":{"source_type":"user_provided","verification_status":"confirmed_by_user"}},
    {"kind":"hotel","extracted":{...},"confirmed":true,"evidence":{...}}
] }
```

> **注入语义**：DD-02 `await_booking_node` 的 `interrupt()` 返回值即此 `resume` 列表，写入 `state["bookings"]`，`stage → confirm`。本模块保证列表中**每条 `confirmed=True`**，否则 BFF 不予 resume。

---

## 5. 核心流程

### 5.1 三入口抽取管道（统一入口分派）

```python
# dd10/service.py（续）
from dd10.extract import run_extract
from dd10.ocr import fetch_screenshot_bytes
from dd10.link import fetch_public_page
from dd10.privacy import mask_pii
from dd10.repo import create_booking, save_draft

async def ingest_booking(plan_id, kind, input_kind, raw) -> dict:
    booking_id = await create_booking(plan_id, kind, input_kind, raw_input=raw)  # confirmed=False
    try:
        if input_kind == "text":
            payload = raw
        elif input_kind == "image":
            payload = await fetch_screenshot_bytes(raw)        # OSS 取字节（§5.2）
        elif input_kind == "link":
            payload = await fetch_public_page(raw)             # 抓公开页正文（§5.3）
        else:                                                  # manual：无抽取，直接返回空表单
            return {"booking_id": booking_id, "kind": kind, "input_kind": "manual",
                    "draft": _empty_draft(kind), "status": "needs_review"}

        facts = await run_extract(kind, input_kind, payload)   # DD-04 → list[Fact]
        draft = _facts_to_draft(kind, facts)                   # 转 §3.3 结构
        draft = mask_pii(kind, draft)                          # 证件/乘客名打码（§9）
        await save_draft(booking_id, draft,
                         evidence={"source_type":"user_provided",
                                   "verification_status":"unknown","note":"needs_review"})
        return {"booking_id": booking_id, "kind": kind, "input_kind": input_kind,
                "draft": draft["fields"],
                "screenshot_signed_url": await _signed_url(raw) if input_kind=="image" else None,
                "low_confidence_fields": [k for k,v in draft["fields"].items()
                                          if v.get("confidence",0) < LOW_CONF_TH],  # 默认 0.75
                "status": "needs_review"}
    except Exception as e:
        # 抽取失败 → 不抛给用户，降级为手工输入（§8）
        await save_draft(booking_id, _empty_draft(kind),
                         evidence={"source_type":"user_provided",
                                   "verification_status":"unknown","note":f"extract_failed:{e}"})
        return {"booking_id": booking_id, "kind": kind, "input_kind": input_kind,
                "draft": _empty_draft(kind)["fields"], "degraded": True,
                "status": "needs_review"}
```

### 5.2 截图入口（Qwen-VL-OCR，OSS）

```python
# dd10/ocr.py
from dd04.oss import get_object, presign_url    # DD-04/DD-01 §9.2 OSS 封装

async def fetch_screenshot_bytes(oss_key: str) -> bytes:
    """从 OSS 取截图字节，交 Qwen-VL-OCR。截图 7 天生命周期由 OSS 规则自动清理。"""
    assert oss_key.startswith("screenshots/"), "非法对象键"
    return await get_object(oss_key)             # 内网直取，不经公网

# 抽取时：run_extract(kind, "image", image_bytes) → extract_fact(task="booking_ocr", ...)
# DD-04 LLM_ROUTES["booking_ocr"] = "qwen-vl-ocr"，多模态直接吃图 + schema 约束输出
```

**OCR 调用要点**：
- 走 DD-04 `extract_fact(task="booking_ocr")`，Qwen-VL-OCR 专为票据/表格结构化设计（v1 §抽取洞察）。
- 图像**不进 `redact()`**（redact 针对文本 payload）；但**抽取出的文本字段立即 `mask_pii`**（乘客名/证件号打码）。
- 抽取完可**提前删截图**（不必等 7 天）；默认 OSS 7 天兜底（DD-01 §9.2）。

### 5.3 链接入口（抓公开页 → 小模型）

```python
# dd10/link.py
from dd04.provider import search_provider     # DD-04 ResilientProvider；禁裸调

ALLOWED_LINK_HOSTS = {   # 仅公开可访问的行程/确认页；不登录、不抓账户页
    "flightaware.com", "variflight.com", "umetrip.com",   # 航班动态公开页
    # 酒店/票务的公开确认页（无需登录的分享链接）
}

async def fetch_public_page(url: str) -> str:
    """仅抓公开页正文（Readability/Jina 清洗）。禁止抓需登录/账户页（隐私+合规）。"""
    host = _host(url)
    if host not in ALLOWED_LINK_HOSTS:
        raise ValueError("链接非公开页白名单，请改用文本/截图回填")   # → 降级手输
    clean_md = await search_provider.fetch_readable(url)      # 走 DD-04，带缓存/限流
    return clean_md
# 抽取：run_extract(kind, "link", clean_md) → extract_fact(task="search_entry", ...)
```

> **合规红线**：链接抓取**仅限公开页**（对齐 DD-04 "搜索仅找入口"、PRD 07 "不读账户/不后台批量抓取"）。12306/航司账户页、需登录的订单页**一律不抓**，引导用户改用截图/文本。

### 5.4 逐字段确认闭环（核心防脏数据）

```python
# dd10/service.py（续）
from dd03.guard import enforce_provenance      # DD-03 §6 闸一
from dd10.normalize import normalize_field
from dd10.repo import mark_confirmed

KEY_FIELDS = {
    "train":  ["train_no","from_station","to_station","date","depart"],
    "flight": ["flight_no","dep_airport","arr_airport","date","dep_time"],
    "hotel":  ["hotel","check_in","check_out"],
}
# DD-03 FIELD_SOURCE_POLICY 字段路径映射
FIELD_PATH = {"train.price":"train.price","train.depart":"train.departure_time",
              "flight.price":"flight.price","flight.dep_time":"flight.dep_time", ...}

async def confirm_booking(plan_id, booking_id, confirmed_fields, partial=False) -> dict:
    b = await load_booking(booking_id)
    kind = b["kind"]
    final = {}
    for name, raw_val in confirmed_fields.items():
        val = normalize_field(kind, name, raw_val)             # 日期/时刻/金额→分 归一
        path = f"{kind}.{name}"
        # 用户确认 → source_type=user_provided → enforce_provenance 返回 confirmed_by_user
        fact = enforce_provenance(FIELD_PATH.get(path, path), val, source_type="user_provided")
        final[name] = {"value": val,
                       "evidence": fact.evidence.model_dump()}  # verification_status=confirmed_by_user

    missing = [f for f in KEY_FIELDS[kind] if f not in confirmed_fields]
    if missing and not partial:
        return {"booking_id": booking_id, "status": "needs_review",
                "missing_key_fields": missing, "ready_for_resume": False}

    all_confirmed = not missing
    if all_confirmed:
        await mark_confirmed(booking_id, extracted={"kind":kind,"fields":final},
                             evidence={"source_type":"user_provided",
                                       "verification_status":"confirmed_by_user",
                                       "confidence":1.0,"note":"用户逐字段确认"})
    return {"booking_id": booking_id, "kind": kind,
            "extracted": {"kind":kind,"fields":final},
            "confirmed": all_confirmed,
            "evidence": {"source_type":"user_provided",
                         "verification_status":"confirmed_by_user" if all_confirmed else "unknown"},
            "ready_for_resume": all_confirmed}
```

**闭环不变量（CI 断言，§11）**：
- 只要有一个关键字段未确认 → `confirmed=False` → 不进 resume → 不进时间线。
- `confirmed=True` 的记录，每个字段 `evidence.verification_status ∈ {confirmed_by_user}`（`price`/`availability` 尤其禁 `llm`，对齐 DD-03 闸三）。

### 5.5 写库与 resume 注入组装

```python
# dd10/resume.py —— BFF 在用户点"完成回填"时调用
from dd10.repo import list_confirmed_bookings

async def build_resume_payload(plan_id: int) -> dict:
    """汇集本 plan 全部 confirmed=True 的 bookings，组装成 DD-02 §6.2 resume 契约。"""
    rows = await list_confirmed_bookings(plan_id)              # WHERE confirmed=True
    return {"resume": [
        {"kind": r["kind"],
         "extracted": r["extracted"],       # {kind, fields:{name:{value,evidence}}}
         "confirmed": True,
         "evidence": r["evidence"]}          # confirmed_by_user
        for r in rows
    ]}
# BFF: payload = await build_resume_payload(plan_id)
#      POST /plans/{id}/resume  → DD-02: graph.invoke(Command(resume=payload["resume"]), cfg)
```

### 5.6 Master Timeline 合并准备（TripIt 式，供 DD-12）

本模块把确认后的 `bookings` 归一为**时间锚点条目**，供 DD-12 `TimelineSolver` 合并进统一时间线（对齐竞品 §5.4 "统一 Master Itinerary 时间线排序"）。**本模块不排程**，只提供规范化的可合并结构：

```python
# dd10/timeline_merge.py —— 供 DD-12 消费（DD-12 读 bookings 也可直接查表）
def to_timeline_anchors(booking) -> list[dict]:
    """把一条 confirmed booking 转成 0~2 个时间锚点（出发/到达 或 入住/离店）。"""
    k, f = booking["kind"], booking["extracted"]["fields"]
    if k == "train":
        return [{"kind":"transport","subtype":"train","ref_table":"bookings","ref_id":booking["id"],
                 "start_at": _dt(f["date"], f["depart"]), "end_at": _dt(f["date"], f["arrive"]),
                 "title": f'{f["train_no"]["value"]} {f["from_station"]["value"]}→{f["to_station"]["value"]}',
                 "from_label": f["from_station"]["value"], "to_label": f["to_station"]["value"],
                 "evidence": booking["evidence"]}]           # confirmed_by_user 透传
    if k == "flight":
        return [{"kind":"transport","subtype":"flight","ref_table":"bookings","ref_id":booking["id"],
                 "start_at": _dt(f["date"], f["dep_time"]), "end_at": _dt(f["date"], f["arr_time"]),
                 "title": f'{f["flight_no"]["value"]} {f["dep_airport"]["value"]}→{f["arr_airport"]["value"]}',
                 "evidence": booking["evidence"]}]
    if k == "hotel":
        return [                                              # 入住/离店两个锚点
            {"kind":"lodging","subtype":"check_in","ref_table":"bookings","ref_id":booking["id"],
             "start_at": _dt(f["check_in"], DEFAULT_CHECKIN_TIME), "title": f'入住 {f["hotel"]["value"]}',
             "evidence": booking["evidence"]},
            {"kind":"lodging","subtype":"check_out","ref_table":"bookings","ref_id":booking["id"],
             "start_at": _dt(f["check_out"], DEFAULT_CHECKOUT_TIME), "title": f'离店 {f["hotel"]["value"]}',
             "evidence": booking["evidence"]}]
    return []
```

> DD-12 `timeline_slots` 用 `ref_table='bookings'` + `ref_id` 指回本表（DD-01 §8.5），锚点的 `evidence` 恒为 `confirmed_by_user`——时间线上"你已确认的车次/航班/酒店"永远是最高可信度。

---

## 6. 与其他模块接线

| 模块 | 关系 | 接线点 |
|---|---|---|
| **DD-04**（OCR/抽取） | 依赖 | 通过 `extract_fact(task, schema, source_type='user_provided')` 调 Qwen-VL-OCR（截图）/小模型（文本/链接）；**禁裸调**；链接抓取走 `ResilientProvider.fetch_readable`（缓存/限流/白名单） |
| **DD-03**（证据护栏） | 依赖 | 确认后每字段经 `enforce_provenance(field,'user_provided')` → `confirmed_by_user`；抽取草稿=`unknown`；`train.*/flight.*` 值来源恒 `user_provided`，天然过闸三 |
| **DD-02**（编排 resume） | 下游 | `build_resume_payload` 产出 §6.2 resume 契约 → BFF `POST /resume` → `Command(resume)` 注入 `state["bookings"]`；写发生在中断期 |
| **DD-09**（预填对照） | 协作 | DD-09 产 `prefill`（去官方平台的预填清单）；本模块在 `ingest_booking` 后做 **prefill vs 回填对照**（车次/日期/时段一致性校验），不一致时前端提示"你填的与建议不同，是否确认？" |
| **DD-12**（时间线消费） | 下游 | `to_timeline_anchors` 提供可合并锚点；DD-12 只读 `bookings WHERE confirmed=True`，绝不读草稿 |
| **DD-13**（确认版 bundle） | 下游 | 消费确认后的 `price` 汇总"已确认花费"；`hotel.cancel_policy` → 生成 `hotel_cancel_deadline` 提醒 |
| **DD-14**（浏览器扩展） | 复用 | 扩展在本地生成结构化草稿（读用户主动选择字段），提交后**走同一 `ingest_booking(input_kind='manual'\|'text')` + `confirm_booking`**；隐私约束见 §9 / PRD 07 |
| **DD-01**（数据/OSS） | 依赖 | 写 `bookings`（唯一写入方）；截图 OSS 布局 §9.2、7 天生命周期、签名 URL |

**prefill 对照实现要点**：

```python
# dd10/prefill_match.py
def match_prefill(kind, draft_fields, prefill: dict) -> dict:
    """回填 vs DD-09 预填清单一致性校验（顺滑度关键：帮用户发现填错）。"""
    diffs = []
    if kind == "train":
        if prefill.get("date") and draft_fields["date"]["value"] != prefill["date"]:
            diffs.append({"field":"date","filled":draft_fields["date"]["value"],"suggested":prefill["date"]})
        # 车次/时段/起终点比对……
    return {"consistent": not diffs, "diffs": diffs}   # diffs 非空→前端标注，仍由用户裁决
```

---

## 7. 证据与状态

| 阶段 | `confirmed` | `evidence.verification_status` | `evidence.note` | 能否进时间线 | 前端渲染（DD-03 §7） |
|---|---|---|---|---|---|
| 抽取中 | False | `unknown` | `ingesting` | ❌ | loading |
| 有草稿待确认 | False | `unknown` | `needs_review` | ❌ | "待确认"占位/黄标 |
| 抽取失败 | False | `unknown` | `extract_failed:...` | ❌ | 空表单，提示手输 |
| 逐字段确认完成 | **True** | **`confirmed_by_user`** | `用户逐字段确认` | ✅ | 实心✓"已确认" |

**铁律**：
1. **抽取产出恒为 `unknown`（needs_review）**，绝不 `confirmed`——抽取只是 OCR/LLM 输出，未经用户确认不可信。
2. **确认后每字段经 `enforce_provenance(field,'user_provided')`**，由 DD-03 定级为 `confirmed_by_user`（`user_provided` → `confirmed_by_user`，见 DD-03 §4 `map_status`）。
3. **禁编兜底**：`train.price`/`flight.price`/`*.availability` 的 `source_type` 永远是 `user_provided`（用户从官方平台回填），永不 `llm`——即使 OCR 误识别，值也来自"用户确认"这一动作，DD-03 闸三（`assert_no_fabricated_transport`）在 DD-02 `compose` 出稿前恒不触发。
4. **草稿不可绕过入库**：DD-12/DD-13 只查 `WHERE confirmed=True`；`needs_review` 记录对下游不可见。

---

## 8. 降级策略

| 失败场景 | 降级路径 | 用户体验 |
|---|---|---|
| **抽取整体失败**（LLM/OCR 异常） | `ingest_booking` 返回空 draft + `degraded=True` → 手工输入表单 | 顺滑退化为手输，不报错、不阻塞 |
| **OCR 识别差**（部分字段乱码/低置信） | 低置信字段（`confidence<0.75`）前端标黄，用户**逐字段编辑** | 只改错项，不必全手输 |
| **链接非白名单/抓取失败** | 拒绝抓取，引导改用截图/文本回填 | 明确提示，不静默失败 |
| **DD-04 配额触顶/熔断** | `ResilientProvider` 已降级；本模块收到 `degraded` 结果照常产草稿 + warning | 抽取可能变粗，仍可确认 |
| **用户只确认部分字段** | `partial=True` 保存已确认，其余留 `needs_review`，`confirmed=False` | 支持分次确认，跨天恢复继续 |
| **manual 入口** | 无抽取，直接给空表单 → 用户填 → 同一确认闭环 | 与三入口共用确认逻辑 |

> 核心原则：**抽取失败绝不阻断回填**。TripIt 式顺滑的底线是"哪怕抽取全挂，用户仍能手输完成回填并进入确认版"。抽取只降低输入量，不是必经路径。

---

## 9. 隐私

严格对齐 DD-01 §11、DD-04 §6.2、PRD 07：

1. **证件号/PII 打码（DD-01 §11）**：
   ```python
   # dd10/privacy.py
   import re
   ID_PAT = re.compile(r'(\d{6})\d{8}(\d{3}[\dXx])')      # 身份证 18 位
   PHONE_PAT = re.compile(r'(1[3-9]\d)\d{4}(\d{4})')
   def mask_pii(kind: str, draft: dict) -> dict:
       """抽取后立即打码：证件号、手机号、乘客真实姓名。落库即脱敏，不存明文。"""
       for name, fld in draft["fields"].items():
           if fld.get("value"):
               fld["value"] = ID_PAT.sub(r'\1********\2', fld["value"])
               fld["value"] = PHONE_PAT.sub(r'\1****\2', fld["value"])
       if "passenger_masked" in draft["fields"]:          # 乘客名只留姓 张*
           draft["fields"]["passenger_masked"]["value"] = _mask_name(...)
       return draft
   ```
   - **证件号不落库**（DD-01 §11.1）：抽取到即打码，`bookings` 不存完整证件号。
   - 进 LLM 前文本先过 DD-04 `redact()`（门牌→商圈、证件→****、预算→区间）。
2. **截图 7 天生命周期（DD-01 §9.2）**：OSS `screenshots/{plan_id}/{uuid}.jpg` 生命周期规则 7 天自动删；抽取完可提前删。
3. **OSS 签名 URL**：截图**不公开可读**，前端预览用**短时效签名 URL**（`presign_url`，默认 5 分钟）。
4. **链接抓取仅公开页**：不抓需登录/账户页（§5.3 白名单）；扩展**默认本地解析、用户确认后才提交**（PRD 07：不自动登录、不读密码支付、不后台批量抓取）。
5. **BYO Key（DD-04 §6.3）**：用户自带 Key 时抽取直连用户配额，服务端不落 Key 明文。

---

## 10. 配置与配额

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `LOW_CONF_TH` | 0.75 | 低于此置信度的字段前端标黄，提示重点核对 |
| `TASK_BY_INPUT.image` | `booking_ocr`(qwen-vl-ocr) | DD-04 LLM_ROUTES；多模态成本最高，单次一图 |
| `TASK_BY_INPUT.text/link` | `activity_extract`/`search_entry`(qwen-turbo) | 小模型，高频低价 |
| OCR 缓存 | 图 sha1 → 抽取结果，TTL 24h | 同图重传不重抽（DD-04 §4.1 抽取类可更长） |
| 链接抓取缓存 | URL → clean_md，TTL 1d | DD-04 搜索类缓存 |
| `quota:llm:{yyyymmdd}` | DD-04 日上限 | 接近上限自动降级为手输提示 |
| OSS 签名 URL 有效期 | 300s | 隐私 |
| 截图生命周期 | 7d | DD-01 §9.2 |

**成本控制**：
- 截图 OCR 每次约 1 张图 token，最贵；文本/链接走小模型。
- 缓存命中（同图/同链接）不重复计费。
- 抽取失败不重试计费类调用（DD-04 §4.2：写/计费类不自动重试）。
- BYO Key 分流重度用户，降低我方成本。

---

## 11. 效果与验收标准（DoD）

### 11.1 验收指标

| 指标 | 目标 | 度量 |
|---|---|---|
| **抽取字段准确率**（关键字段） | train/flight ≥ 90%，hotel ≥ 85% | 截图/文本样例集人工标注比对 |
| **确认闭环无脏数据入库** | `bookings WHERE confirmed=True AND 任一字段 evidence!=confirmed_by_user` = **0** | CI SQL 断言 |
| **未确认不入时间线** | `timeline_slots` 引用的 booking 全部 `confirmed=True` | CI 断言（联合 DD-12） |
| **禁编** | `train.*/flight.*/*.availability` 来源=`llm` 的记录 = 0 | DD-03 闸三 CI 用例 |
| **回填顺滑度** | 截图回填中位耗时 ≤ 15s（上传→确认完成） | 埋点 P50 |
| **抽取失败降级率** | 失败必落手输表单，0 阻塞 | 混沌测试 |
| **隐私** | `bookings` 无完整证件号明文；截图 7 天后不可访问 | 扫描 + OSS 规则检查 |

### 11.2 测试用例

```python
# tests/dd10/
# —— 样例集：12306 车票截图、航司(东航/国航)行程单截图、酒店(携程/Booking)确认单截图 ——
def test_ocr_train_12306_screenshot():
    # 输入：12306 购票成功页截图 → 断言 train_no/date/depart 准确率 ≥90%
    ...
def test_ocr_flight_airline_screenshot():
    # 输入：东航 App 行程单截图 → flight_no/dep_time/dep_airport 抽取正确
    ...
def test_ocr_hotel_confirmation_screenshot():
    # 输入：携程酒店确认单截图 → hotel/check_in/check_out/cancel_policy 抽取
    ...
def test_text_paste_train():
    # 输入："G7016 上海虹桥08:12→苏州08:35 二等座¥39.5 7月25日" → 结构化
    ...
def test_confirm_loop_no_dirty_data():
    # 抽取后未确认 → confirmed=False；缺关键字段 → ready_for_resume=False（脏数据零入库）
    ...
def test_confirmed_evidence_is_user_confirmed():
    # 确认后每字段 evidence.verification_status == confirmed_by_user
    ...
def test_resume_payload_matches_dd02_contract():
    # build_resume_payload 输出结构 == DD-02 §6.2 {"resume":[{kind,extracted,confirmed,evidence}]}
    ...
def test_extract_failure_degrades_to_manual():
    # mock extract_fact 抛异常 → 返回空 draft + degraded=True，不抛错
    ...
def test_link_non_whitelist_rejected():
    # 非公开页白名单链接 → ValueError → 引导手输
    ...
def test_pii_masked_before_persist():
    # 含身份证/手机号的抽取 → 落库已打码，无明文
    ...
def test_low_confidence_flagged():
    # confidence<0.75 字段进 low_confidence_fields
    ...
def test_prefill_mismatch_flagged():
    # 回填日期与 DD-09 prefill 不一致 → diffs 非空（顺滑提醒）
    ...
```

---

## 12. 开发任务拆解与风险

### 12.1 任务拆解

1. `bookings` repo（create/save_draft/mark_confirmed/list_confirmed）+ 状态语义（0.5d）
2. 三类抽取 schema（TrainExtract/FlightExtract/HotelExtract，PydanticAI）+ `_facts_to_draft`（1d）
3. `ingest_booking` 三入口分派 + 文本/截图/链接路径（接 DD-04 `extract_fact`）（1.5d）
4. 截图入口：OSS 直传/取字节/签名 URL + Qwen-VL-OCR 接线（1d）
5. 链接入口：公开页白名单 + `fetch_readable`（0.5d）
6. `confirm_booking` 逐字段确认 + 归一化 + `enforce_provenance` 定级（1d）
7. `build_resume_payload`（对齐 DD-02 §6.2）+ BFF resume 对接（0.5d）
8. `to_timeline_anchors` 合并结构（供 DD-12）（0.5d）
9. `mask_pii` 打码 + `prefill_match` 对照（0.5d）
10. 样例集采集 + 验收测试（含 12306/航司/酒店截图）（1.5d）

### 12.2 风险与缓解

| 风险 | 缓解 |
|---|---|
| OCR 对 12306/航司多样版式识别不稳 | 逐字段确认兜底（准确率不足由用户改）；低置信标黄；积累样例集迭代 prompt |
| 用户嫌确认麻烦（顺滑度） | 高置信字段预勾选、只标黄需核对项；对齐 prefill 减少填错；截图入口一键上传 |
| 抽取直接入时间线（越权） | 铁律：DD-12 只读 `confirmed=True`；CI SQL 断言零脏数据；`needs_review` 下游不可见 |
| LLM 编造票价/余票 | 值来源恒 `user_provided`；DD-03 闸三 CI 红线用例 |
| 截图/PII 泄露 | 7 天生命周期 + 签名 URL + 落库前打码 + 不抓账户页 |
| 链接抓取踩合规红线 | 仅公开页白名单，非白名单拒绝并引导手输 |
| 跨天恢复时草稿丢失 | 草稿也落 `extracted`（`confirmed=False`），用户隔日可继续编辑确认 |

---

> 本文的 `ingest_booking`/`confirm_booking` 接口、`bookings.extracted` schema、resume 注入格式（对齐 DD-02 §6.2）、`to_timeline_anchors` 合并结构为本模块对外契约。任何变更须回改本文并通知 DD-02/DD-12/DD-13/DD-14。**抽取只是初稿，确认才是事实。**
