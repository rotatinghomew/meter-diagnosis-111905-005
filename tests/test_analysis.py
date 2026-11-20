"""业务规则单元测试：状态重建、指标口径、许可结论、值守与换房、反事实对照。"""
import unittest
from pathlib import Path

from roomload.events import reconstruct
from roomload.licensing import (
    counterfactual_closed_as_zero, day2_attribution, license_table,
)
from roomload.loading import load, parse_hhmm
from roomload.metrics import (
    CONFIRMED, DENIED, UNKNOWN, all_conditions, build_block_stats,
    collect_evidence, evaluate,
)
from roomload.scheduling import (
    BLOCKED, CONTROLLED_TRIAL, OK, assess_window, evaluate_swap,
    operational_events, staff_can_hold_closed,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load(FIXTURES)
        cls.facts = reconstruct(cls.ds)
        cls.verdicts = evaluate(cls.ds, cls.facts)
        cls.vmap = {v.condition.key: v for v in cls.verdicts}
        cls.rows, _ = license_table(cls.ds)

    # ---------- 加载与替代链 ----------
    def test_supersessions_are_applied(self):
        self.assertEqual(self.ds.superseded_observations, {"M2-2-HALL-1"})
        self.assertEqual(self.ds.superseded_events, {"H3", "H4"})
        ids = {s.id for s in self.ds.segments}
        self.assertIn("MC", ids)              # 替代记录生效
        self.assertNotIn("M2-2-HALL-1", ids)  # 旧观测作废
        event_ids = {e.id for e in self.ds.events}
        self.assertEqual({"H6", "H7"} & event_ids, {"H6", "H7"})

    # ---------- 时钟不确定下的状态重建 ----------
    def test_k22_door_uncertainty_then_determined_open(self):
        atoms = self.facts["K2-2"].atoms
        early = next(a for a in atoms if a.start == parse_hhmm("11:00"))
        self.assertEqual(set(early.door), {"closed", "open"})   # 窗口内两种解释
        late = next(a for a in atoms if a.start == parse_hhmm("11:12"))
        self.assertEqual(late.door, ("open",))                  # 11:12 后确定开门

    def test_k22_activity_split_removes_determined_coexistence(self):
        bf = self.facts["K2-2"]
        a_end = next(e for e in bf.events
                     if e.kind == "activity_end" and e.target == "A")
        b_start = next(e for e in bf.events
                       if e.kind == "activity_start" and e.target == "B")
        # A 最晚结束时刻 <= B 最早启动时刻：拆分后不存在任何“两者都已确定在场”的区间
        self.assertLessEqual(a_end.end, b_start.start)
        for atom in bf.atoms:
            if atom.start >= b_start.start:
                # B 启动窗口及以后：A 在每个可行解释中都已结束
                self.assertNotIn(True, set(atom.a_active))
        # 11:12 之后事实唯一：A 不在、B 在、门开
        late = next(a for a in bf.atoms if a.start == parse_hhmm("11:12"))
        self.assertEqual(late.a_active, (False,))
        self.assertEqual(late.b_active, (True,))
        self.assertEqual(late.door, ("open",))
        # 拆分后 K2-2 不能为任何 eq=0 组合条件提供证据
        bundle = collect_evidence(self.ds, self.facts)
        for key, days in bundle["evidence"].items():
            if key[2] is False:
                self.assertNotIn("K2-2", [e.block_id for e in days.values()])

    def test_h7_makes_equipment_determined_at_1108(self):
        atoms = self.facts["K2-2"].atoms
        at = next(a for a in atoms if a.start == parse_hhmm("11:08"))
        self.assertEqual(at.equipment, (True,))

    def test_k21_schedule_facts_unchanged_by_feedback(self):
        atoms = self.facts["K2-1"].atoms
        self.assertEqual(len(atoms), 1)
        self.assertEqual(atoms[0].door, ("open",))
        self.assertEqual(atoms[0].equipment, (False,))

    # ---------- 指标口径 ----------
    def test_over_minutes_use_covered_denominator(self):
        stats, _ = build_block_stats(self.ds, self.facts)
        k64 = stats["K6-4"]
        self.assertNotIn("EAST", k64)              # 零覆盖，不造一个 0/30
        self.assertEqual(k64["HALL"].over, 0)
        k12 = stats["K1-2"]
        for p in ("A_IN", "B_IN", "HALL", "EAST"):
            self.assertEqual(k12[p].ratio, 1.0)    # 覆盖分钟全部超线

    def test_mc_replacement_lowers_k22_hall_over_minutes(self):
        stats, _ = build_block_stats(self.ds, self.facts)
        # 旧值 58.6 会超 50；MC 改为 48 后 HALL 超线 25/30
        self.assertEqual(stats["K2-2"]["HALL"].over, 25)

    def test_indoor_and_corridor_points_are_not_substitutable(self):
        # P1 与 P2 分别审议：K6-4 缺 EAST 时 P1 不能拿 HALL 顶替
        ab_closed = self.vmap[("AB", "closed", False)]
        p1 = ab_closed.group_results["P1"]
        self.assertTrue(p1["coverage_incomplete"])
        self.assertEqual(p1["status"], UNKNOWN)
        p2 = ab_closed.group_results["P2"]
        self.assertEqual(p2["status"], CONFIRMED)

    # ---------- 许可结论 ----------
    def test_core_license_outcomes(self):
        expected = {
            ("AB", "open", False): DENIED,
            ("AB", "closed", False): CONFIRMED,
            ("A", "open", False): CONFIRMED,
            ("A", "closed", False): CONFIRMED,
            ("B", "open", False): CONFIRMED,
            ("B", "closed", False): UNKNOWN,       # 零覆盖
        }
        for key, status in expected.items():
            self.assertEqual(self.vmap[key].status, status, key)

    def test_all_equipment_on_conditions_unknown_without_same_equipment_baseline(self):
        for cond in all_conditions():
            if not cond.equipment_on:
                continue
            self.assertEqual(self.vmap[cond.key].status, UNKNOWN, cond.key)
            self.assertTrue(
                any("同设备状态的无活动基线" in r for r in self.vmap[cond.key].reasons),
                cond.key,
            )

    def test_min_paired_days_gate(self):
        for v in self.verdicts:
            if v.status in (CONFIRMED, DENIED):
                self.assertGreaterEqual(v.paired_days, 3)

    def test_k22_does_not_feed_eq0_conditions(self):
        bundle = collect_evidence(self.ds, self.facts)
        for key, days in bundle["evidence"].items():
            if key[2] is False:
                self.assertNotIn("K2-2", [e.block_id for e in days.values()])

    def test_k22_ab_evidence_only_under_uncertain_eq1_window(self):
        bundle = collect_evidence(self.ds, self.facts)
        ab_open_eq1 = bundle["evidence"][("AB", "open", True)]
        self.assertIn(2, ab_open_eq1)
        self.assertEqual(ab_open_eq1[2].block_id, "K2-2")
        self.assertLess(ab_open_eq1[2].feasible_minutes, 30)  # 只覆盖开门可行窗口

    # ---------- 归因范围 ----------
    def test_day2_attribution_ranges(self):
        attr = day2_attribution(self.ds)
        # A 最多解释 5 分钟（11:00-11:03 不确定段对应的一个 5 分钟观测）
        self.assertEqual(attr["attributable_ranges"]["A 活动"]["A_IN"], (0, 5))
        # B 与通风设备对全部超线分钟都可行 → 无法区分单源/叠加
        self.assertEqual(attr["attributable_ranges"]["B 活动"]["A_IN"], (30, 30))
        self.assertEqual(attr["attributable_ranges"]["通风设备"]["A_IN"], (30, 30))
        self.assertEqual(attr["over_minutes"]["HALL"], (25, 30))

    # ---------- 值守与借道 ----------
    def test_passage_forbids_closed_door(self):
        ok, _ = staff_can_hold_closed(self.ds, "D1", 11,
                                      parse_hhmm("10:00"), parse_hhmm("11:00"))
        self.assertFalse(ok)  # S3 借道 10:20-10:45

    def test_sequential_duties_by_same_person_are_allowed(self):
        # S1 10:00-10:30 守 D1，S2 10:30 起守 D2：取借道（10:20）之前的窗口验证
        ok, detail = staff_can_hold_closed(self.ds, "D1", 11,
                                           parse_hhmm("10:00"), parse_hhmm("10:20"))
        self.assertTrue(ok, detail)

    def test_simultaneous_two_door_duty_blocks_closing(self):
        ok, detail = staff_can_hold_closed(self.ds, "D1", 12,
                                           parse_hhmm("11:30"), parse_hhmm("12:00"))
        self.assertFalse(ok)
        self.assertIn("D2", detail)

    def test_day11_parallel_ab_is_blocked_by_passage_and_denied_license(self):
        w = assess_window(self.ds, 11, parse_hhmm("10:00"), parse_hhmm("10:30"),
                          ("A", "B"), self.vmap)
        self.assertEqual(w.decision, BLOCKED)
        self.assertIn("S3", w.passage_blockers)

    def test_day12_parallel_ab_only_controlled_trial(self):
        w = assess_window(self.ds, 12, parse_hhmm("11:30"), parse_hhmm("12:00"),
                          ("A", "B"), self.vmap)
        self.assertEqual(w.decision, CONTROLLED_TRIAL)

    def test_single_room_bookings_remain_confirmed(self):
        w = assess_window(self.ds, 11, parse_hhmm("10:45"), parse_hhmm("11:15"),
                          ("B",), self.vmap)
        self.assertEqual(w.decision, OK)
        self.assertEqual(w.license_status, CONFIRMED)

    # ---------- 换房三要素 ----------
    def test_swap_evaluation(self):
        b5 = next(b for b in self.ds.bookings if b.id == "B5")
        sw = evaluate_swap(self.ds, b5, self.vmap)
        self.assertTrue(sw["size_ok"])                       # large -> C(large)
        self.assertEqual(sw["conflicts"], [])                # C 窗口无已签收占用
        self.assertEqual(sw["window"].decision, CONTROLLED_TRIAL)
        b10 = next(b for b in self.ds.bookings if b.id == "B10")
        self.assertEqual(b10.kind, "declined")

    # ---------- 反事实对照 ----------
    def test_operational_door_event_on_booking_day(self):
        events = operational_events(self.ds, 11)
        self.assertEqual([e.id for e in events], ["H8"])
        self.assertEqual(events[0].target, "D1")
        self.assertEqual(events[0].start, parse_hhmm("10:05"))
        self.assertEqual(operational_events(self.ds, 12), [])

    # ---------- 反事实对照 ----------
    def test_counterfactual_covers_four_failure_modes(self):
        findings = counterfactual_closed_as_zero(self.ds)
        self.assertEqual(len(findings), 4)
        texts = " ".join(f["case"] + f["wrong_effect"] for f in findings)
        for token in ("K2-2", "K6-4", "B 单间闭门", "设备开启"):
            self.assertIn(token, texts)


if __name__ == "__main__":
    unittest.main()
