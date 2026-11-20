"""排期与值守判定。

规则（DATA.md 与任务约定）：
- signed / performed 是不可当成空房的既有安排；requested / offered 是待决申请；
  declined 已被拒绝；prior 指向被调整的原预约（offered/declined/performed 都挂回原单）。
- 单房间合格不代表相邻房间能同时排；A、B 共用走廊门 D1，组合许可以 licensing 结论为准。
- 关门是有现场人员能够落实的条件，不是预约备注：同一人同时只能负责一扇门；
  passage=yes 的借道通行时段不得闭门。
- 临时换房分别判断：社团所需房型、重叠窗口、被换出房间的后续开放。
- 证据不足的组合允许“受控试用”，但不能抢占已确认活动，也不能伪装成正式开放。
- 新证据只卡住相关组合和时间，其他已确认预约继续有效。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .loading import Booking, Dataset, StaffDuty, fmt_hhmm
from .metrics import CONFIRMED, DENIED, UNKNOWN

CONTROLLED_TRIAL = "controlled_trial"
BLOCKED = "blocked"
OK = "ok"


@dataclass
class WindowAssessment:
    day: int
    start: int
    end: int
    rooms: tuple[str, ...]
    combo: str
    door_needed: str
    license_status: str
    passage_blockers: list[str]
    staff_feasible: bool
    staff_detail: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    sub_windows: list = field(default_factory=list)


def _overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    return s1 < e2 and s2 < e1


def duties_on(ds: Dataset, day: int, start: int, end: int) -> list[StaffDuty]:
    return [d for d in ds.duties if d.day == day and _overlap(start, end, d.start, d.end)]


def passage_windows(ds: Dataset, door: str, day: int, start: int, end: int) -> list[StaffDuty]:
    return [d for d in duties_on(ds, day, start, end) if d.target == door and d.passage]


def staff_can_hold_closed(ds: Dataset, door: str, day: int,
                          start: int, end: int) -> tuple[bool, str]:
    """关门条件能否由现场人员在整个窗口落实。

    同一人同时只能负责一扇门：逐分钟检查该门是否有人、且此人此刻没有负责别的门。
    同一人先后守不同门（如先 D1 后 D2）是允许的。
    """
    overlapping = duties_on(ds, day, start, end)
    blockers = [d for d in overlapping if d.target == door and d.passage]
    if blockers:
        spans = ", ".join(f"{fmt_hhmm(d.start)}-{fmt_hhmm(d.end)}" for d in blockers)
        return False, f"借道通行 {spans} 期间 {door} 不得闭门"

    holders = [d for d in overlapping if d.target == door and d.resource]
    if not holders:
        return False, f"窗口内没有可负责 {door} 门控的现场人员"

    other_door_duties = [
        d for d in overlapping if d.target != door and d.resource and not d.passage
    ]
    for minute in range(start, end):
        on_this = [d for d in holders if d.start <= minute < d.end]
        if not on_this:
            return False, f"{door} 门控在 {fmt_hhmm(minute)} 无人值守"
        for d in on_this:
            clash = [x for x in other_door_duties
                     if x.resource == d.resource and x.start <= minute < x.end]
            if clash:
                other = ", ".join(sorted({x.target for x in clash}))
                return False, (f"{d.resource} 在 {fmt_hhmm(minute)} 同时负责 {other}，"
                               f"无法守 {door}")
    who = ", ".join(sorted({d.resource for d in holders}))
    return True, f"关门条件可由 {who} 落实"


def _sub_windows(ds: Dataset, door: str, day: int,
                 start: int, end: int) -> list[tuple[int, int, bool]]:
    """按借道通行把窗口切成 (lo, hi, passage_active) 子窗口。"""
    cuts = {start, end}
    for d in passage_windows(ds, door, day, start, end):
        cuts.add(max(d.start, start))
        cuts.add(min(d.end, end))
    cuts = sorted(cuts)
    out = []
    for lo, hi in zip(cuts, cuts[1:]):
        active = any(
            d.target == door and d.passage and d.start <= lo and d.end >= hi
            for d in duties_on(ds, day, lo, hi)
        )
        out.append((lo, hi, active))
    return out


def _assess_sub(ds: Dataset, door: str, combo: str, day: int, lo: int, hi: int,
                passage_active: bool, open_verdict, closed_verdict) -> dict:
    reasons: list[str] = []
    if passage_active:
        reasons.append(f"{fmt_hhmm(lo)}-{fmt_hhmm(hi)} 借道通行，{door} 必须开放，关门方案不可用")
        if open_verdict.status == CONFIRMED:
            return {"decision": OK, "door": "open", "license": CONFIRMED,
                    "staff_ok": True, "detail": "通行时段无需关门值守", "reasons": reasons}
        if open_verdict.status == DENIED:
            reasons.append("开门条件已被试运行拒绝")
            return {"decision": BLOCKED, "door": "open", "license": DENIED,
                    "staff_ok": True, "detail": "通行时段无需关门值守", "reasons": reasons}
        reasons.append("开门条件证据不足，只能受控试用且不得抢占已确认活动")
        return {"decision": CONTROLLED_TRIAL, "door": "open", "license": UNKNOWN,
                "staff_ok": True, "detail": "通行时段无需关门值守", "reasons": reasons}

    staff_ok, detail = staff_can_hold_closed(ds, door, day, lo, hi)
    if closed_verdict.status == CONFIRMED and staff_ok:
        reasons.append(f"关门条件已确认；{detail}")
        return {"decision": OK, "door": "closed", "license": CONFIRMED,
                "staff_ok": True, "detail": detail, "reasons": reasons}
    if open_verdict.status == CONFIRMED:
        reasons.append("开门条件已确认")
        if closed_verdict.status != CONFIRMED:
            reasons.append("关门条件未确认：" + "；".join(closed_verdict.reasons))
        elif not staff_ok:
            reasons.append(f"关门无法落实：{detail}")
        return {"decision": OK, "door": "open", "license": CONFIRMED,
                "staff_ok": staff_ok, "detail": detail, "reasons": reasons}
    if closed_verdict.status == CONFIRMED and not staff_ok:
        reasons.append(f"关门许可成立但现场无法落实：{detail}；只能受控试用")
        return {"decision": CONTROLLED_TRIAL, "door": "closed", "license": UNKNOWN,
                "staff_ok": False, "detail": detail, "reasons": reasons}
    reasons.append("开门/关门均无充分确认证据，受控试用")
    return {"decision": CONTROLLED_TRIAL, "door": "unknown", "license": UNKNOWN,
            "staff_ok": staff_ok, "detail": detail, "reasons": reasons}


def assess_window(ds: Dataset, day: int, start: int, end: int,
                  rooms: tuple[str, ...], verdict_map: dict[tuple, object]) -> WindowAssessment:
    """评估某窗口内一组房间同时使用的可行性（支持 A/B 组合、单间与 C 房）。

    含借道通行的窗口切成子窗口分别判定；任一子窗口不可行则整窗不可行，
    报告会指出卡在哪个子窗口，而不是把整段预约一刀切。
    """
    room_set = set(rooms)
    if room_set == {"A", "B"}:
        combo, door = "AB", "D1"
    elif room_set == {"A"}:
        combo, door = "A", "D1"
    elif room_set == {"B"}:
        combo, door = "B", "D1"
    elif room_set == {"C"}:
        combo, door = "C", "D2"
    else:
        raise ValueError(f"unsupported room set {rooms}")

    reasons: list[str] = []
    passage = passage_windows(ds, door, day, start, end)
    passage_blockers = [d.id for d in passage]

    if combo == "C":
        if passage:
            spans = ", ".join(f"{d.id} {fmt_hhmm(d.start)}-{fmt_hhmm(d.end)}" for d in passage)
            reasons.append(f"借道通行 {spans}：D2 保持开放")
        reasons.append("C 房没有满足 min_paired_days 的同日配对证据，只能受控试用，"
                       "不得抢占 A/B 的已确认预约")
        return WindowAssessment(day, start, end, rooms, combo, "open",
                                UNKNOWN, passage_blockers, True,
                                "C 房（D2）无组合门控约束",
                                CONTROLLED_TRIAL, reasons)

    open_verdict = verdict_map[(combo, "open", False)]
    closed_verdict = verdict_map[(combo, "closed", False)]
    eq1_open = verdict_map[(combo, "open", True)]
    eq1_closed = verdict_map[(combo, "closed", True)]

    subs = []
    for lo, hi, active in _sub_windows(ds, door, day, start, end):
        r = _assess_sub(ds, door, combo, day, lo, hi, active, open_verdict, closed_verdict)
        r.update({"lo": lo, "hi": hi, "passage": active})
        subs.append(r)

    order = {BLOCKED: 3, CONTROLLED_TRIAL: 2, OK: 1}
    worst = max(subs, key=lambda r: order[r["decision"]])
    for r in subs:
        tag = "借道开放段" if r["passage"] else "可关门段"
        reasons.append(f"【{tag} {fmt_hhmm(r['lo'])}-{fmt_hhmm(r['hi'])}】")
        reasons.extend("  " + x for x in r["reasons"])
    for v, tag in ((eq1_open, "设备开启+开门"), (eq1_closed, "设备开启+闭门")):
        if v.status == UNKNOWN:
            reasons.append(f"备注：{tag}条件无同日同设备基线，保持未知（见试验缺口）")

    return WindowAssessment(
        day, start, end, rooms, combo, worst["door"], worst["license"],
        passage_blockers, worst["staff_ok"], worst["detail"],
        worst["decision"], reasons, sub_windows=subs,
    )


def operational_events(ds: Dataset, day: int) -> list:
    """预约日的现场转态事件（无候选 block，直接作为门控事实）。"""
    return [e for e in ds.events if e.day == day and not e.candidates]


def evaluate_swap(ds: Dataset, offer: Booking, verdict_map: dict[tuple, object]) -> dict:
    """临时降级换房的三要素判断：房型匹配、重叠窗口、被换出房间的后续开放。"""
    target_room = ds.rooms[offer.room]
    notes = []
    size_ok = target_room.size == offer.size_need
    notes.append(
        f"房型：{offer.group}需要 {offer.size_need}，{offer.room} 是 {target_room.size} -> "
        + ("匹配" if size_ok else "不匹配")
    )

    # 重叠窗口：同房间同时段已签收/已执行的安排不能被抢占
    conflicts = []
    for b in ds.bookings:
        if b.id == offer.id or b.room != offer.room or b.day != offer.day:
            continue
        if b.kind in ("signed", "performed") and _overlap(offer.start, offer.end, b.start, b.end):
            conflicts.append(b)
    if conflicts:
        notes.append("重叠窗口冲突：" + "，".join(
            f"{b.id}（{b.group} {b.kind}）" for b in conflicts))
    else:
        notes.append(f"重叠窗口：{offer.room} 房 {fmt_hhmm(offer.start)}-{fmt_hhmm(offer.end)} "
                     "无已签收/已执行占用")

    # 被换出房间（原预约 prior 所在房间）的后续开放
    prior = next((b for b in ds.bookings if b.id == offer.prior), None)
    freed_room = ds.rooms[prior.room] if prior else None
    if prior:
        notes.append(
            f"被换出房间 {prior.room}（{freed_room.size}）在原窗口 "
            f"{fmt_hhmm(prior.start)}-{fmt_hhmm(prior.end)} 仍归 {prior.group} 的 {prior.id}，"
            "换房不成立时原预约继续有效"
        )

    window = assess_window(ds, offer.day, offer.start, offer.end, (offer.room,), verdict_map)
    notes.append(f"目标房许可：{window.decision}（{window.license_status}）")
    feasible = size_ok and not conflicts and window.decision != BLOCKED
    return {
        "offer": offer,
        "size_ok": size_ok,
        "conflicts": conflicts,
        "window": window,
        "feasible": feasible,
        "notes": notes,
    }


def reconcile_bookings(ds: Dataset, verdict_map: dict[tuple, object]):
    """把 bookings.csv 的每条记录解释成面向社团与经理的决定。"""
    results = []
    for b in ds.bookings:
        note: list[str] = []
        if b.kind == "signed":
            decision = "existing_signed"
        elif b.kind == "performed":
            decision = "performed_attribution_only"
            note.append("已执行：只能更正归因，不能重记为未发生")
        elif b.kind == "declined":
            decision = "rejected"
            if b.prior:
                note.append(f"拒绝的是 {b.prior} 的换房提议，原预约继续有效")
        elif b.kind == "offered":
            decision = "offer_under_review"
        else:
            decision = "request_under_review"
        results.append({"booking": b, "decision": decision, "notes": note})
    return results
