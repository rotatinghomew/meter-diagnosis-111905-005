"""指标计算：覆盖分钟、超线分钟比例、同日同设备基线与增量上限。

指标定义（thresholds.csv + 任务约定）：
- 干扰条件：点位读数达到（>=）该组 cutoff 即计入超线分钟。
- 指标 = 相关点位达到干扰条件的分钟 / 该点位组被覆盖的分钟；未覆盖分钟不允许当零。
- 点位组内所有点位都在界内，该组才算通过；室内点（A_IN/B_IN）与走廊点（HALL）
  及东侧点（EAST）不能互相替代。
- increase_cap：与“同日、同设备状态的无活动 block”比较时，超线分钟比例允许的增量上限。
- 只有覆盖满 min_paired_days 且所有仍成立的解释都不超限，才能按条件确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .events import BlockFacts
from .loading import Dataset, Segment, Threshold

NO_ACTIVITY = "none"          # a_on=b_on=0
COMBO_A = "A"
COMBO_B = "B"
COMBO_AB = "AB"

CONFIRMED = "confirmed"
DENIED = "denied"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Condition:
    """一个许可条件：房间组合 × 门状态 × 设备状态。"""
    combo: str
    door: str
    equipment_on: bool

    @property
    def key(self) -> tuple[str, str, bool]:
        return (self.combo, self.door, self.equipment_on)


@dataclass
class PointStat:
    point: str
    covered: int = 0
    over: int = 0

    @property
    def ratio(self) -> float | None:
        return None if self.covered == 0 else self.over / self.covered


@dataclass
class BlockDayEvidence:
    """某 block 对某条件提供的证据（只含与该条件相容的观测段）。"""
    block_id: str
    day: int
    point_stats: dict[str, PointStat] = field(default_factory=dict)
    feasible_minutes: int = 0     # block 内该条件可行的总分钟（含未覆盖部分）
    observed_minutes: int = 0     # 其中真正有点位覆盖的分钟
    ambiguity: str = ""


@dataclass
class ConditionVerdict:
    condition: Condition
    status: str                   # confirmed / denied / unknown
    paired_days: int
    block_days: list[BlockDayEvidence]
    baseline_days: dict[int, str]
    group_results: dict[str, dict]   # group_id -> 统计与判定明细
    reasons: list[str] = field(default_factory=list)


def _combo_of(a: bool, b: bool) -> str:
    if a and b:
        return COMBO_AB
    if a:
        return COMBO_A
    if b:
        return COMBO_B
    return NO_ACTIVITY


def all_conditions() -> list[Condition]:
    out = []
    for combo in (COMBO_A, COMBO_B, COMBO_AB):
        for door in ("open", "closed"):
            for equipment_on in (False, True):
                out.append(Condition(combo, door, equipment_on))
    return out


def segment_feasible(facts: BlockFacts, seg: Segment,
                     door: str, equipment_on: bool, a_on: bool, b_on: bool) -> bool:
    """该 5 分钟观测段全程都与给定状态相容，才可作为该条件的证据。

    段落在转态不确定窗口内时，只有该状态在窗口内始终可行才算相容；
    这保证同一观测段可以同时支持多个仍成立的解释，但不会被强行唯一归因。
    """
    for atom in facts.atoms:
        if atom.start < seg.end and atom.end > seg.start:
            if not (door in atom.door and equipment_on in atom.equipment
                    and a_on in atom.a_active and b_on in atom.b_active):
                return False
    return True


def point_cutoff_map(ds: Dataset) -> dict[str, float]:
    out: dict[str, float] = {}
    for th in ds.thresholds.values():
        for p in th.points:
            out[p] = th.cutoff
    return out


def build_block_stats(ds: Dataset, facts: dict[str, BlockFacts]):
    """返回 block -> point -> PointStat（只统计未被替代的观测段）。"""
    cut = point_cutoff_map(ds)
    stats: dict[str, dict[str, PointStat]] = {}
    segs_by_block: dict[str, list[Segment]] = {}
    for seg in ds.segments:
        segs_by_block.setdefault(seg.block, []).append(seg)
    for bid, segs in segs_by_block.items():
        per_point: dict[str, PointStat] = {}
        for seg in segs:
            st = per_point.setdefault(seg.point, PointStat(seg.point))
            st.covered += seg.end - seg.start
            if seg.level >= cut[seg.point]:
                st.over += seg.end - seg.start
        stats[bid] = per_point
    return stats, segs_by_block


def _combo_members(combo: str) -> tuple[bool, bool]:
    return {"A": (True, False), "B": (False, True), "AB": (True, True)}[combo]


def collect_evidence(ds: Dataset, facts: dict[str, BlockFacts]) -> dict[tuple, dict]:
    """对每个条件收集 block-day 证据；同时登记无活动基线 block。"""
    _, segs_by_block = build_block_stats(ds, facts)
    evidence: dict[tuple, dict[int, BlockDayEvidence]] = {
        c.key: {} for c in all_conditions()
    }
    # baselines: (equipment_on, door) -> day -> block_id；仅登记状态确定的无活动 block
    baselines: dict[tuple[bool, str], dict[int, str]] = {}

    for bid, bf in facts.items():
        trial = bf.trial
        segs = segs_by_block.get(bid, [])
        possible = bf.possible_states()
        combos = {_combo_of(a, b) for (_, _, a, b) in possible}
        if combos == {NO_ACTIVITY}:
            for (door, equipment_on, _, _) in possible:
                baselines.setdefault((equipment_on, door), {})[trial.day] = bid
        for cond in all_conditions():
            a_on, b_on = _combo_members(cond.combo)
            matching_atoms = [
                atom for atom in bf.atoms
                if (cond.door in atom.door and cond.equipment_on in atom.equipment
                    and a_on in atom.a_active and b_on in atom.b_active)
            ]
            if not matching_atoms:
                continue
            ev = BlockDayEvidence(block_id=bid, day=trial.day)
            ev.feasible_minutes = sum(atom.minutes for atom in matching_atoms)
            ambiguous = any(
                len(atom.door) > 1 or len(atom.equipment) > 1
                or len(atom.a_active) > 1 or len(atom.b_active) > 1
                for atom in bf.atoms
                if (cond.door in atom.door and cond.equipment_on in atom.equipment
                    and a_on in atom.a_active and b_on in atom.b_active)
            )
            ev.ambiguity = "clock/state uncertainty" if ambiguous else ""
            per_point: dict[str, PointStat] = {}
            feasible_segments = [
                seg for seg in segs
                if segment_feasible(bf, seg, cond.door, cond.equipment_on, a_on, b_on)
            ]
            for seg in feasible_segments:
                st = per_point.setdefault(seg.point, PointStat(seg.point))
                st.covered += seg.end - seg.start
                if seg.level >= point_cutoff_map(ds)[seg.point]:
                    st.over += seg.end - seg.start
            ev.observed_minutes = sum(seg.end - seg.start for seg in feasible_segments)
            ev.point_stats = per_point
            evidence[cond.key][trial.day] = ev
    return {"evidence": evidence, "baselines": baselines}


def evaluate(ds: Dataset, facts: dict[str, BlockFacts]) -> list[ConditionVerdict]:
    bundle = collect_evidence(ds, facts)
    evidence = bundle["evidence"]
    baselines = bundle["baselines"]
    # 无活动基线按设备状态索引（门状态不影响“同设备状态”要求，记录实际门状态供说明）
    baseline_by_eq: dict[bool, dict[int, str]] = {}
    for (equipment_on, _door), day_map in baselines.items():
        merged = baseline_by_eq.setdefault(equipment_on, {})
        for day, bid in day_map.items():
            merged.setdefault(day, bid)

    block_stats, _ = build_block_stats(ds, facts)
    verdicts: list[ConditionVerdict] = []

    for cond in all_conditions():
        days = evidence[cond.key]
        paired: dict[int, BlockDayEvidence] = {}
        used_baselines: dict[int, str] = {}
        for day, ev in sorted(days.items()):
            base_id = baseline_by_eq.get(cond.equipment_on, {}).get(day)
            if base_id is not None:
                paired[day] = ev
                used_baselines[day] = base_id

        group_results: dict[str, dict] = {}
        reasons: list[str] = []
        for gid, th in ds.thresholds.items():
            entry = _evaluate_group(cond, th, paired, used_baselines, block_stats)
            group_results[gid] = entry

        has_baseline_gap = any(
            cond.equipment_on not in baseline_by_eq
            or day not in baseline_by_eq[cond.equipment_on]
            for day in days
        )
        if not days:
            reasons.append("没有任何 block 与该条件相容（零覆盖）")
        if has_baseline_gap:
            reasons.append(
                "存在试验日缺少同日、同设备状态的无活动基线"
                + ("（设备开启日只能用同设备基线，不能用白天设备关闭的观测替代）"
                   if cond.equipment_on else "")
            )
        coverage_gap = [
            ev.block_id for ev in days.values()
            if any(
                ev.point_stats.get(p) is None or ev.point_stats[p].covered == 0
                for th in ds.thresholds.values() for p in th.points
            )
        ]
        if coverage_gap:
            reasons.append(f"点位覆盖不完整的 block：{', '.join(sorted(set(coverage_gap)))}")

        min_days = max(th.min_paired_days for th in ds.thresholds.values())
        if paired and len(paired) >= min_days:
            denied_groups = [g for g, r in group_results.items() if r["status"] == DENIED]
            if denied_groups:
                status = DENIED
                reasons.insert(0, f"超线增量在 {', '.join(denied_groups)} 组超过 increase_cap")
            else:
                status = CONFIRMED
                if has_baseline_gap or coverage_gap:
                    reasons.append("结论仅覆盖有基线且覆盖完整的试验日，其余时段保持未知")
        else:
            status = UNKNOWN
            if not paired:
                reasons.append("可配对试验日为 0")
            else:
                reasons.append(
                    f"可配对试验日 {len(paired)} 天，少于 min_paired_days={min_days}"
                )

        verdicts.append(ConditionVerdict(
            condition=cond,
            status=status,
            paired_days=len(paired),
            block_days=list(days.values()),
            baseline_days=used_baselines,
            group_results=group_results,
            reasons=reasons,
        ))
    return verdicts


def _evaluate_group(cond: Condition, th: Threshold,
                    paired: dict[int, BlockDayEvidence],
                    used_baselines: dict[int, str],
                    block_stats: dict[str, dict[str, PointStat]]) -> dict:
    per_day = []
    any_violation = False
    coverage_incomplete = False
    for day, ev in sorted(paired.items()):
        base_id = used_baselines[day]
        day_entry = {"day": day, "block": ev.block_id, "baseline": base_id, "points": {}}
        for p in th.points:
            st = ev.point_stats.get(p)
            base_st = block_stats.get(base_id, {}).get(p)
            if st is None or st.covered == 0 or base_st is None or base_st.covered == 0:
                coverage_incomplete = True
                day_entry["points"][p] = None
                continue
            base_ratio = base_st.over / base_st.covered
            increase = st.over / st.covered - base_ratio
            within = increase <= th.increase_cap + 1e-12
            any_violation = any_violation or not within
            day_entry["points"][p] = {
                "covered": st.covered,
                "over": st.over,
                "ratio": st.over / st.covered,
                "baseline_ratio": base_ratio,
                "increase": increase,
                "within_cap": within,
            }
        per_day.append(day_entry)
    if not paired or coverage_incomplete:
        status = UNKNOWN
    elif any_violation:
        status = DENIED
    else:
        status = CONFIRMED
    return {"status": status, "days": per_day, "coverage_incomplete": coverage_incomplete}
