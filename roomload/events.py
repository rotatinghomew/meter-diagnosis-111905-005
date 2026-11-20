"""把转态事件应用到试运行 block，重建事实时间线。

关键规则（DATA.md 与任务约定）：
- trials.csv 的 door/on/equipment 是记档安排；“状态事实优先于排期”，
  door_open / power_on / activity_start / activity_end 等转态事件给出事实。
- 转态事件只能归入符合时钟的候选 block（candidate 为空且时间落在 block 内也可归入）。
- 事件 from/to 是不确定窗口：from==to 表示时点确定，否则转态在 [from,to] 内任一时刻成立，
  所有与时钟相容的解释都保留，不武断选一个。
- supersedes 到达后旧记录作废（加载阶段已剔除），新记录通常缩小不确定窗口。
- 校对与后补记录只能缩小有效区间，不把整个配对批次删掉。
- feedback 事件只描述居民反馈覆盖区间，不直接改变门/活动/设备状态。
"""
from __future__ import annotations

from dataclasses import dataclass

from .loading import Dataset, Event, Trial


@dataclass(frozen=True)
class Atom:
    """时间线上的原子区间 [start,end)，区间内各状态的可行取值集合不变。"""
    start: int
    end: int
    door: tuple[str, ...]          # 可行门状态
    equipment: tuple[bool, ...]    # 可行设备状态
    a_active: tuple[bool, ...]
    b_active: tuple[bool, ...]
    note: str = ""

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def feasible_combos(self) -> set[tuple[str, bool, bool, bool]]:
        return {(d, e, a, b) for d in self.door for e in self.equipment
                for a in self.a_active for b in self.b_active}


@dataclass
class BlockFacts:
    trial: Trial
    atoms: list[Atom]
    events: list[Event]
    feedback: list[Event]

    def possible_states(self) -> set[tuple[str, bool, bool, bool]]:
        out: set[tuple[str, bool, bool, bool]] = set()
        for atom in self.atoms:
            out |= atom.feasible_combos()
        return out


def _eligible(block: Trial, event: Event) -> bool:
    if event.day != block.day:
        return False
    if event.candidates:
        return block.id in event.candidates
    # 无候选名单时，要求事件窗口与 block 时钟相交
    return event.start < block.end and event.end > block.start


def reconstruct(ds: Dataset) -> dict[str, BlockFacts]:
    facts: dict[str, BlockFacts] = {}
    events_by_block: dict[str, list[Event]] = {}
    feedback_by_block: dict[str, list[Event]] = {}
    for event in ds.events:
        target_buckets = (
            [c for c in event.candidates if c in ds.trials]
            if event.candidates
            else [b.id for b in ds.trials.values() if _eligible(b, event)]
        )
        for bid in target_buckets:
            if event.kind == "feedback":
                feedback_by_block.setdefault(bid, []).append(event)
            else:
                events_by_block.setdefault(bid, []).append(event)

    for bid, trial in ds.trials.items():
        facts[bid] = _reconstruct_one(
            trial,
            events_by_block.get(bid, []),
            feedback_by_block.get(bid, []),
        )
    return facts


def _dimension_values(initial: bool | str,
                      transitions: list[tuple[Event, bool | str]],
                      mid: int) -> tuple:
    """单个状态维度在时刻 mid 的可行取值（保留原始类型）。

    transitions 为 (事件, 转态后取值)，按事件窗口排序。转态在 [from,to] 内任一时刻成立：
    - mid <= from：只有初值；
    - from < mid < to：转态前后两种值都可行；
    - mid >= to：只有转态后取值；from==to 的即时转态在 mid>=from 即生效。
    """
    values = {initial}
    for ev, after in transitions:
        if ev.start == ev.end:
            if mid >= ev.start:
                values = {after}
        elif mid <= ev.start:
            break
        elif mid < ev.end:
            values = {initial, after}
        else:
            values = {after}
    return tuple(sorted(values, key=str))


def _reconstruct_one(trial: Trial, events: list[Event],
                     feedback: list[Event]) -> BlockFacts:
    # 时间轴切分点：block 边界 + 所有事件窗口的 from/to（裁剪到 block 内）
    cuts = {trial.start, trial.end}
    for ev in events:
        if ev.start > trial.start:
            cuts.add(max(ev.start, trial.start))
        if ev.end < trial.end:
            cuts.add(min(ev.end, trial.end))
    cuts = sorted(c for c in cuts if trial.start <= c <= trial.end)

    door_trans = [(ev, "open") for ev in events if ev.kind == "door_open"]
    power_trans = [(ev, True) for ev in events if ev.kind == "power_on"]
    a_trans = [(ev, ev.kind == "activity_start")
               for ev in events
               if ev.kind in ("activity_start", "activity_end") and ev.target == "A"]
    b_trans = [(ev, ev.kind == "activity_start")
               for ev in events
               if ev.kind in ("activity_start", "activity_end") and ev.target == "B"]

    atoms: list[Atom] = []
    for lo, hi in zip(cuts, cuts[1:]):
        mid = (lo + hi) // 2
        note_ids = [ev.id for ev in events if ev.start < hi and ev.end > lo]
        atoms.append(Atom(
            start=lo, end=hi,
            door=_dimension_values(trial.door, door_trans, mid),
            equipment=_dimension_values(trial.equipment_on, power_trans, mid),
            a_active=_dimension_values(trial.a_on, a_trans, mid),
            b_active=_dimension_values(trial.b_on, b_trans, mid),
            note=";".join(note_ids),
        ))
    return BlockFacts(trial=trial, atoms=atoms, events=events, feedback=feedback)
