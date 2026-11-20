"""组合许可汇总：把指标结论、门控可落实性和事件归因合成最终许可表。

- 关门许可同时受走廊通行约束：有借道通行的时段，闭门条件即使数据合格也不能排。
- 不同源同开时，只有资料能区分单源和叠加影响，才允许给单间房出结论；
  无法区分的区间保留为多种共同成立的解释（见 day-2 K2-2 归因）。
- 提供“把关闭时段误当零超线”的对照分析，展示该错误会污染哪些开放决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import reconstruct
from .loading import Dataset, fmt_hhmm
from .metrics import (
    Condition, ConditionVerdict,
    build_block_stats, evaluate, point_cutoff_map,
    CONFIRMED,
)


@dataclass
class LicenseRow:
    condition: Condition
    metric_status: str
    paired_days: int
    door_enforceable: bool | None
    final_status: str
    reasons: list[str] = field(default_factory=list)


def build_verdict_map(verdicts: list[ConditionVerdict]) -> dict[tuple, ConditionVerdict]:
    return {v.condition.key: v for v in verdicts}


def license_table(ds: Dataset) -> tuple[list[LicenseRow], dict[tuple, ConditionVerdict]]:
    facts = reconstruct(ds)
    verdicts = evaluate(ds, facts)
    vmap = build_verdict_map(verdicts)
    rows: list[LicenseRow] = []
    for v in verdicts:
        cond = v.condition
        reasons = list(v.reasons)
        enforceable: bool | None = None
        final = v.status
        if cond.door == "closed" and v.status == CONFIRMED:
            # 关门是现场条件：常规开放时段（09:00-15:00）的 D1 通行窗口需要逐窗口核验；
            # 这里给出门控本身是否具备可落实性的通用结论，具体窗口在排期层判定。
            blockers = [
                d for d in ds.duties if d.target == "D1" and d.passage
            ]
            if blockers:
                enforceable = False
                spans = "; ".join(
                    f"第{d.day}日 {fmt_hhmm(d.start)}-{fmt_hhmm(d.end)}" for d in blockers
                )
                reasons.append(f"闭门许可受借道通行限制（{spans}），这些窗口只能按开放方案排")
                final = CONFIRMED  # 数据许可仍成立，但带适用窗口限制
            else:
                enforceable = True
        rows.append(LicenseRow(
            condition=cond,
            metric_status=v.status,
            paired_days=v.paired_days,
            door_enforceable=enforceable,
            final_status=final,
            reasons=reasons,
        ))
    return rows, vmap


@dataclass
class AttributionSpan:
    label: str
    earliest: int
    latest: int
    detail: str


def day2_attribution(ds: Dataset) -> dict:
    """第 2 日 K2-2 的后补记录拆分与可归因范围（分钟级，保留相容解释）。"""
    facts = reconstruct(ds)
    bf = facts["K2-2"]
    cut = point_cutoff_map(ds)
    block_stats, _ = build_block_stats(ds, facts)

    # 转态不确定窗口（事件 from/to 裁剪到 block）
    door_event = next(e for e in bf.events if e.kind == "door_open")
    vent_event = next(e for e in bf.events if e.kind == "power_on")
    a_end_event = next(e for e in bf.events
                       if e.kind == "activity_end" and e.target == "A")
    b_start_event = next(e for e in bf.events
                         if e.kind == "activity_start" and e.target == "B")

    # 状态“确定成立”的区间：可行集是只含该值的单元素元组。
    # 逐段输出而不是合并成 [最早,最晚]，避免把夹着不确定窗口的两段误显示成连续区间。
    def only(values, value):
        return len(values) == 1 and values[0] == value

    def determined_runs(predicate, not_before: int | None = None) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for atom in bf.atoms:
            if not_before is not None and atom.start < not_before:
                continue
            if predicate(atom):
                if runs and runs[-1][1] == atom.start:
                    runs[-1] = (runs[-1][0], atom.end)
                else:
                    runs.append((atom.start, atom.end))
        return runs

    door_runs = determined_runs(lambda a: only(a.door, "open"), door_event.end)
    vent_runs = determined_runs(lambda a: only(a.equipment, True), vent_event.start)
    a_runs = determined_runs(lambda a: only(a.a_active, True))
    b_runs = determined_runs(lambda a: only(a.b_active, True), b_start_event.end)

    # 超线读数按可行来源拆分（HALL 已应用 MC 替代）
    hall = block_stats["K2-2"]["HALL"]
    a_in = block_stats["K2-2"]["A_IN"]
    b_in = block_stats["K2-2"]["B_IN"]
    east = block_stats["K2-2"]["EAST"]

    # 各来源能够解释的超线分钟上下界。
    # 对每个超线 5 分钟段，看它落入哪些原子区间、该来源在这些区间是否可行：
    #   下界 = 来源在该段全程确定可行（所有重叠原子都可行）
    #   上界 = 来源在该段至少一个原子可行（解释可以共同成立）
    source_predicates = {
        "A 活动": lambda a: True in a.a_active,
        "B 活动": lambda a: True in a.b_active,
        "通风设备": lambda a: True in a.equipment,
    }

    def over_minutes_in_atoms(predicate, point: str) -> tuple[int, int]:
        segs = [s for s in ds.segments if s.block == "K2-2" and s.point == point]
        low = high = 0
        for seg in segs:
            if seg.level < cut[point]:
                continue
            atoms = [a for a in bf.atoms if a.start < seg.end and a.end > seg.start]
            if not atoms:
                continue
            mins = seg.end - seg.start
            if all(predicate(a) for a in atoms):
                low += mins
            if any(predicate(a) for a in atoms):
                high += mins
        return low, high

    # 确定性独占区间（分钟）：来源在该原子区间唯一确定成立，且其他来源确定不成立。
    determined = {
        "A 活动": lambda a: only(a.a_active, True),
        "B 活动": lambda a: only(a.b_active, True),
        "通风设备": lambda a: only(a.equipment, True),
    }
    absent = {
        "A 活动": lambda a: only(a.a_active, False),
        "B 活动": lambda a: only(a.b_active, False),
        "通风设备": lambda a: only(a.equipment, False),
    }

    def certain_exclusive(source: str) -> int:
        others = [o for o in determined if o != source]
        total = 0
        for atom in bf.atoms:
            if determined[source](atom) and all(absent[o](atom) for o in others):
                total += atom.minutes
        return total

    a_ranges = {p: over_minutes_in_atoms(source_predicates["A 活动"], p)
                for p in ("A_IN", "B_IN", "HALL")}
    b_ranges = {p: over_minutes_in_atoms(source_predicates["B 活动"], p)
                for p in ("A_IN", "B_IN", "HALL")}
    vent_ranges = {p: over_minutes_in_atoms(source_predicates["通风设备"], p)
                   for p in ("A_IN", "B_IN", "HALL")}
    exclusive = {
        "A 活动": certain_exclusive("A 活动"),
        "B 活动": certain_exclusive("B 活动"),
        "通风设备": certain_exclusive("通风设备"),
    }
    return {
        "block": "K2-2",
        "event_windows": [
            AttributionSpan("D1 开门转态窗口（H2）", door_event.start, door_event.end,
                            "窗口内开门/关门两种解释都成立；窗口外门确定为关"),
            AttributionSpan("通风设备开启时点（H7 替代 H3）", vent_event.start, vent_event.end,
                            "H7 把窗口缩为 11:08 单点，11:08 起设备确定开启"),
            AttributionSpan("A 活动结束时点（H6 替代 H4）", a_end_event.start, a_end_event.end,
                            "11:03 单点确定，A 早于排期 11:30 结束"),
            AttributionSpan("B 活动启动窗口（H5）", b_start_event.start, b_start_event.end,
                            "窗口内 B 未启/已启两种解释都成立；11:12 后 B 确定在进行"),
        ],
        "determined_runs": [
            {"label": "D1 确定开放", "runs": door_runs, "note": "开门不确定窗口 11:12 结束之后"},
            {"label": "通风设备确定运行", "runs": vent_runs, "note": "H7 即时转态 11:08 起"},
            {"label": "A 活动确定进行", "runs": a_runs, "note": "H6 即时结束 11:03 之前"},
            {"label": "B 活动确定进行", "runs": b_runs, "note": "B 启动不确定窗口 11:12 结束之后"},
        ],
        "over_minutes": {
            "A_IN": (a_in.over, a_in.covered),
            "B_IN": (b_in.over, b_in.covered),
            "HALL": (hall.over, hall.covered),
            "EAST": (east.over, east.covered),
        },
        "attributable_ranges": {
            "A 活动": a_ranges,
            "B 活动": b_ranges,
            "通风设备": vent_ranges,
        },
        "exclusive_minutes": exclusive,
        "conclusions": [
            "排期名义的 A+B 同时活动在 K2-2 从未发生：A 于 11:03 确定结束，B 最早 11:05 才开始",
            "K2-2 不能再作为 A+B 闭门（设备开）证据；门在 11:00-11:12 窗口内可能开着",
            "11:08 后设备与 B 同开，资料无法区分单源与叠加，该段不得用于给 B 单间出结论",
            "已执行的 30 分钟观测与超线读数保留，只能更正归因，不能重记为未发生",
            "居民反馈 H1 覆盖 10:50-11:15，横跨 K2-1 与 K2-2，两种来源解释共同成立",
        ],
    }


def counterfactual_closed_as_zero(ds: Dataset) -> list[dict]:
    """对照：若把所有关门/未覆盖时段错误当成“零超线”，会改变哪些决定。"""
    facts = reconstruct(ds)
    stats, _ = build_block_stats(ds, facts)
    findings = []

    # 1) K2-2 名义闭门但事实开门且设备开：误当零会凭空造出一个合格的 A+B 闭门 eq1 样本
    findings.append({
        "case": "K2-2（名义 closed/eq1，事实 11:00-11:12 可能开门、设备 11:08 起开）",
        "error": "把关门标签当成零超线",
        "actual": "A_IN/B_IN/EAST 各 30/30 超线，HALL 25/30（MC 替代后）",
        "wrong_effect": "会虚构 A+B 闭门+设备开的合格样本，错误放行设备开启时的 A+B 闭门预约",
    })

    # 2) K6-4 缺 EAST 覆盖：误当零会让 P1 组在第 6 日“通过”，凑满配对覆盖
    findings.append({
        "case": "K6-4（A+B 闭门，EAST 点位零覆盖）",
        "error": "把未提供观测的 EAST 当成零超线",
        "actual": "EAST 覆盖 0 分钟，指标无定义；该日不能计入 P1 配对日",
        "wrong_effect": "会把覆盖缺口伪装成合格日，提前确认 A+B 闭门许可并掩盖试验缺口",
    })

    # 3) 全部设备开启 block（K2-2/K2-3/K5-2/K5-3）没有同日 eq1 无活动基线
    findings.append({
        "case": "设备开启 block 群（K2-2,K2-3,K5-2,K5-3）",
        "error": "用白天设备关闭的零超线基线代替晚间/同设备基线",
        "actual": "无任何 (eq=1, 无活动) 基线，增量无法估计，4 个 eq1 条件全部未知",
        "wrong_effect": "会把设备背景造成的超线错误记到活动/门状态头上，错误拒绝或确认单间条件",
    })

    # 4) B 闭门条件零覆盖：误当零会直接“确认”B 闭门
    findings.append({
        "case": "B 单间闭门（全部 8 个试验日零覆盖）",
        "error": "把没有闭门 B 试验当成闭门零超线",
        "actual": "没有任何 block 与 B+closed 相容，状态为未知",
        "wrong_effect": "会凭空确认 B 闭门许可，排期时给阅读/手工组安排无法兑现的闭门时段",
    })
    return findings
