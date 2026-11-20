"""把分析结果渲染成 Markdown 报告与机读 JSON。

产出（reports/ 下）：
- license_report.md：组合许可总表、指标明细、未知条件与下一次试验要求。
- day2_attribution.md：第 2 日后补记录拆分、可归因范围与反馈的多解释处理。
- schedule_report.md：第 11-12 日预约决定、值守/借道约束、换房三要素与社团视图。
- counterfactual.md：把关闭/未覆盖时段误当零超线会污染的开放决定。
- results.json：上述全部结论的机读版本。
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .licensing import (
    LicenseRow, counterfactual_closed_as_zero, day2_attribution, license_table,
)
from .loading import Dataset, fmt_hhmm
from .metrics import CONFIRMED, DENIED, UNKNOWN
from .scheduling import (
    BLOCKED, CONTROLLED_TRIAL, OK, assess_window, evaluate_swap,
    operational_events, reconcile_bookings,
)

STATUS_CN = {CONFIRMED: "确认", DENIED: "拒绝", UNKNOWN: "未知"}
DECISION_CN = {OK: "可直接确认", CONTROLLED_TRIAL: "仅受控试用", BLOCKED: "不可排"}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def license_markdown(ds: Dataset, rows: list[LicenseRow], vmap) -> str:
    lines = [
        "# 房间组合许可报告",
        "",
        "> 指标：相关点位达到场地自定干扰条件（读数 ≥ cutoff）的分钟占该组**已覆盖**分钟的比例；",
        "> 未覆盖不等于零。增量上限 increase_cap 只与**同日、同设备状态的无活动 block**比较。",
        "> 点位组 P1=HALL/EAST（cutoff 50，cap 0.12，≥3 配对日）；",
        "> P2=A_IN/B_IN（cutoff 54，cap 0.20，≥3 配对日）。室内点与走廊点不互代。",
        "",
        "## 1. 许可总表（设备关 eq=0 / 设备开 eq=1）",
        "",
        "| 房间组合 | 门 | 设备 | 指标结论 | 配对日 | 门控可落实性 | 最终状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r.condition
        enforce = {True: "可落实", False: "受通行/值守限制", None: "不适用"}[r.door_enforceable]
        lines.append(
            f"| {c.combo} | {c.door} | eq={int(c.equipment_on)} | "
            f"{STATUS_CN[r.metric_status]} | {r.paired_days} | {enforce} | "
            f"{STATUS_CN[r.final_status]} |"
        )

    lines += [
        "",
        "## 2. 已确认条件的指标明细（eq=0）",
        "",
        "| 条件 | 点位组 | 配对日 | 各日超线比例（活动块 vs 同日基线） | 最大增量 | cap |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.condition.equipment_on or r.metric_status != CONFIRMED:
            continue
        for gid, gr in vmap[r.condition.key].group_results.items():
            per_day = []
            max_inc = 0.0
            for d in gr["days"]:
                for p, cell in d["points"].items():
                    if cell:
                        max_inc = max(max_inc, cell["increase"])
                per_day.append(
                    f"第{d['day']}日({d['block']}): "
                    + "/".join(
                        f"{p}={_pct(c['ratio']) if c else '缺覆盖'}"
                        for p, c in d["points"].items()
                    )
                )
            th = ds.thresholds[gid]
            lines.append(
                f"| {r.condition.combo}+{r.condition.door} | {gid} | "
                f"{len(gr['days'])} | {'；'.join(per_day)} | {_pct(max_inc)} | "
                f"{_pct(th.increase_cap)} |"
            )

    lines += [
        "",
        "## 3. 被拒绝条件",
        "",
    ]
    denied = [r for r in rows if r.metric_status == DENIED]
    for r in denied:
        lines.append(f"- **{r.condition.combo} + {r.condition.door} + eq=0：拒绝**")
        for reason in r.reasons:
            lines.append(f"  - {reason}")
        for gid, gr in vmap[r.condition.key].group_results.items():
            cap = ds.thresholds[gid].increase_cap
            # 汇总：全部配对日、全部点位都超 cap，只给范围与一个示例
            increases = [
                cell["increase"] for d in gr["days"] for cell in d["points"].values() if cell
            ]
            example = next(
                (d for d in gr["days"]
                 if any(cell and not cell["within_cap"] for cell in d["points"].values())),
                None,
            )
            lines.append(
                f"  - {gid}：{len(gr['days'])} 个配对日全部超限，增量区间 "
                f"{_pct(min(increases))}–{_pct(max(increases))}（cap {_pct(cap)}）；"
                f"示例 第{example['day']}日 {example['block']}："
                + "，".join(
                    f"{p} {_pct(c['ratio'])}（基线 {_pct(c['baseline_ratio'])}）"
                    for p, c in example["points"].items() if c
                )
            )

    lines += ["", "## 4. 未知条件与下一次需要试什么", ""]
    unknown = [r for r in rows if r.metric_status == UNKNOWN]
    lines.append("| 条件 | 缺口 | 下一次试验要求 |")
    lines.append("|---|---|---|")
    next_trial = {
        ("B", "closed", False): "安排至少 3 个 B 单间闭门 block（同日含无活动基线、全点位覆盖）",
        ("B", "closed", True): "先补 eq=0 闭门 B，再排同设备 eq=1 配对",
    }
    for r in unknown:
        key = r.condition.key
        if r.condition.equipment_on:
            demand = ("同日增设设备开启的无活动基线 block（不能用白天 eq=0 观测替代），"
                      "再排同设备状态的活动 block，至少 3 个配对日")
        else:
            demand = next_trial.get(key, "补足同日无活动基线与全点位覆盖，至少 3 个配对日")
        gap = "；".join(r.reasons)
        lines.append(f"| {r.condition.combo}+{r.condition.door}+eq={int(r.condition.equipment_on)} "
                     f"| {gap} | {demand} |")

    lines += [
        "",
        "## 5. 适用窗口限制",
        "",
        "- A+B 闭门与单间闭门的数据许可成立，但**借道通行窗口不得闭门**：",
        "  第11日 10:20-10:45（S3，D1）、第12日 11:30-12:00（S6，D2）。",
        "- 关门必须有现场人员全程值守，同一人同一时刻只能负责一扇门；",
        "  第12日 11:30-12:00 U2 被排在 D2（S5），无法同时落实 D1 闭门。",
        "- 单房间合格不等于相邻房间可同时排：A、B 单间开门/闭门均确认，",
        "  但 A+B 开门被拒绝，A+B 同时使用只能走闭门方案且受通行窗口限制。",
    ]
    return "\n".join(lines) + "\n"


def attribution_markdown(attr: dict) -> str:
    lines = [
        "# 第 2 日 K2-2 后补记录拆分与归因",
        "",
        "## 1. 转态事件（已应用 supersedes：H6 替代 H4、H7 替代 H3）",
        "",
        "| 事件 | 不确定/确定时点 | 含义 |",
        "|---|---|---|",
    ]
    for t in attr["event_windows"]:
        span = (fmt_hhmm(t.earliest) if t.earliest == t.latest
                else f"{fmt_hhmm(t.earliest)}–{fmt_hhmm(t.latest)}")
        lines.append(f"| {t.label} | {span} | {t.detail} |")
    lines += [
        "",
        "## 2. 状态确定成立的区间（不确定窗口内的两种解释不写成确定值）",
        "",
        "| 状态 | 确定区间 | 备注 |",
        "|---|---|---|",
    ]
    for item in attr["determined_runs"]:
        runs = "，".join(f"{fmt_hhmm(lo)}–{fmt_hhmm(hi)}" for lo, hi in item["runs"]) or "无"
        lines.append(f"| {item['label']} | {runs} | {item['note']} |")

    lines += [
        "",
        "## 3. 超线分钟（HALL 已用 MC 替代 M2-2-HALL-1）",
        "",
        "| 点位 | 超线/覆盖 |",
        "|---|---|",
    ]
    for p, (over, cov) in attr["over_minutes"].items():
        lines.append(f"| {p} | {over}/{cov}（{_pct(over / cov)}） |")

    lines += [
        "",
        "## 4. 各来源可解释的超线分钟范围（下界=全程可行，上界=窗口内可行）",
        "",
        "| 来源 | A_IN | B_IN | HALL |",
        "|---|---|---|---|",
    ]
    for src, pts in attr["attributable_ranges"].items():
        cell = lambda p: f"{pts[p][0]}–{pts[p][1]} 分钟"
        lines.append(f"| {src} | {cell('A_IN')} | {cell('B_IN')} | {cell('HALL')} |")
    lines += [
        "",
        "解释：",
        "- A 活动 11:03 已确定结束，只在 11:00-11:03 的不确定窗口内可行，",
        "  最多解释 5 分钟超线（HALL 该窗口读数 57.6 ≥50）。",
        "- B 活动与通风设备在全部超线分钟内都可行；11:08 后二者同开，",
        "  资料无法区分单源与叠加，因此不给 B 单间在该 block 下结论。",
        "- 不存在某来源“独占且其他来源确定不成立”的超线区间（exclusive=0），",
        "  任何把 30 分钟全部记给单一来源的说法都不被资料支持。",
        "",
        "## 5. 结论（经理三问）",
        "",
    ]
    for c in attr["conclusions"]:
        lines.append(f"- {c}")
    lines += [
        "",
        "### 对房间组合许可的影响",
        "- K2-2 原本挂在 A+B 闭门+设备开条件下；拆分后该 block 不含任何 A+B 同时活动区间，",
        "  门还在 11:00-11:12 可能开着，因此不能支撑该条件——该条件本来就因缺同设备基线为未知，结论不变（仍未知）。",
        "- 既有 **A+B 开门拒绝、A+B 闭门确认、单间确认** 的结论由其他 7-8 个试验日支撑，不受影响；",
        "  新证据只卡住 K2-2 相关的 eq=1 条件与时段。",
        "",
        "### 并排活动的替代选项",
        "- 第 11 日同时段 A 排练（B1）与 B 阅读（B2）：10:20-10:45 借道强制开门，",
        "  而 A+B 开门被拒绝 → 该并排不可排；可选：把 B2 移出 10:20 之后的窗口，",
        "  或在 10:00-10:20 闭门段内完成重叠部分（U1 在岗可落实 D1 闭门）。",
        "",
        "### 已执行活动的处理",
        "- B4（10:00-10:25 排练，performed）与 K2-2 的 30 分钟观测都已发生：",
        "  只能按上面的来源范围更正归因，不能重记成没发生，读数与超线分钟保留。",
    ]
    return "\n".join(lines) + "\n"


def schedule_markdown(ds: Dataset, vmap) -> str:
    lines = [
        "# 第 11–12 日排期决定",
        "",
        "口径：signed/performed 是不可当空房的既有安排；controlled_trial 不得抢占已确认活动、",
        "不得伪装成正式开放；新证据只卡住相关组合与时间。",
        "",
        "## 1. 窗口评估",
        "",
        "| 日 | 窗口 | 房间 | 组合 | 门方案 | 许可 | 决定 |",
        "|---|---|---|---|---|---|---|",
    ]
    requests = [
        (11, 600, 630, ("A", "B"), "B1+B2 并排"),
        (11, 600, 660, ("A",), "B1 排练（signed）"),
        (11, 600, 630, ("B",), "B2 阅读（signed）"),
        (11, 600, 625, ("A",), "B4 已执行段（performed）"),
        (11, 630, 660, ("C",), "B5 换房提议（offered）"),
        (11, 645, 675, ("B",), "B6 手工（requested）"),
        (12, 660, 720, ("A",), "B7 排练（requested）"),
        (12, 690, 720, ("A", "B"), "B7+B8 并排"),
        (12, 690, 720, ("B",), "B8 阅读（requested）"),
        (12, 690, 720, ("C",), "B9 讲解（requested）"),
    ]
    details = []
    for day, s, e, rooms, label in requests:
        w = assess_window(ds, day, s, e, rooms, vmap)
        lines.append(
            f"| {day} | {fmt_hhmm(s)}-{fmt_hhmm(e)} | {'+'.join(rooms)} | {w.combo} | "
            f"{w.door_needed} | {STATUS_CN[w.license_status]} | {DECISION_CN[w.decision]} |"
        )
        details.append((label, w))
    lines += ["", "## 2. 窗口判定明细", ""]
    for label, w in details:
        lines.append(f"### {label}（第{w.day}日 {fmt_hhmm(w.start)}-{fmt_hhmm(w.end)}）")
        for r in w.reasons:
            lines.append(f"- {r}")
        lines.append("")

    lines += ["## 3. 换房三要素：B5（排练组 A→C，offered；B10 为同一提议的 declined 记录）", ""]
    b5 = next(b for b in ds.bookings if b.id == "B5")
    sw = evaluate_swap(ds, b5, vmap)
    for n in sw["notes"]:
        lines.append(f"- {n}")
    lines += [
        f"- 综合：{'可行但只能受控试用' if sw['feasible'] else '不可行'}；"
        "B10 已 declined 表示该换房未生效，B1（A 房 signed）继续有效。",
        "",
        "## 4. 现场转态事实（值班员记录，状态事实优先于排期）",
        "",
    ]
    op_lines = []
    for day in (11, 12):
        for e in operational_events(ds, day):
            span = (fmt_hhmm(e.start) if e.start == e.end
                    else f"{fmt_hhmm(e.start)}-{fmt_hhmm(e.end)}")
            op_lines.append(
                f"- 第{day}日 {e.id}：{e.target} 在 {span} 记录 {e.kind}（{e.received} 收到）"
            )
    lines += op_lines or ["- 预约日无额外现场转态记录"]
    lines += [
        "- H8 表明第11日 10:05 D1 实际处于开放：B4（排练，performed）与 B2（阅读）",
        "  并行期间走廊门为开；该事实用于事后归因（与 A+B 开门被拒绝的试验结论一致），",
        "  不能反过来把该时段当成闭门或无影响的干净对照。",
        "",
        "## 5. 值守与借道通行核算",
        "",
        "- S1 第11日 10:00-10:30 U1 守 D1；S2 10:30-11:00 U1 守 D2：同人先后不同门，允许。",
        "- S3 第11日 10:20-10:45 借道 D1（passage=yes）：该窗口 D1 禁止闭门。",
        "- S4 第12日 11:00-12:00 U2 守 D1；S5 11:30-12:00 U2 守 D2：11:30 起同人撞两门，",
        "  D1 闭门在 11:30-12:00 无法落实。",
        "- S6 第12日 11:30-12:00 借道 D2：D2 保持开放。",
        "",
        "## 6. 社团可见视图（有效安排与变更原因）",
        "",
        "| 社团 | 有效安排 | 变更/原因 |",
        "|---|---|---|",
        "| 排练组 | 第11日 A 10:00-11:00（B1，signed）；B4 10:00-10:25 已执行 | "
        "换到 C 的 B5 仅受控试用、B10 已拒绝；原预约不变 |",
        "| 阅读组 | 第11日 B 10:00-10:30（B2，signed）；第12日 B 11:30-12:00（B8）可确认（开门） | "
        "与排练并排的 10:20-10:30 段因借道+开门拒绝需缩短或错峰 |",
        "| 讲解组 | 第11日 C 10:00-10:30（B3，signed）；第12日 C（B9）仅受控试用 | "
        "C 房无配对证据，且 11:30-12:00 D2 借道须开放 |",
        "| 手工组 | 第11日 B 10:45-11:15（B6）开门可确认 | "
        "B 闭门零覆盖，不能承诺闭门；10:45 后 D1 无值守 |",
    ]
    return "\n".join(lines) + "\n"


def counterfactual_markdown(findings: list[dict]) -> str:
    lines = [
        "# 对照：把关闭/未覆盖时段误当“零超线”的后果",
        "",
        "下列每条都说明错误口径会怎样改变开放决定；正确口径下这些时段保持未知或按实测计入。",
        "",
    ]
    for i, f in enumerate(findings, 1):
        lines += [
            f"## {i}. {f['case']}",
            f"- 错误做法：{f['error']}",
            f"- 实际资料：{f['actual']}",
            f"- 被污染的决定：{f['wrong_effect']}",
            "",
        ]
    return "\n".join(lines)


def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serializable: {type(obj)}")


def write_reports(ds: Dataset, out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, vmap = license_table(ds)
    attr = day2_attribution(ds)
    findings = counterfactual_closed_as_zero(ds)

    paths = {
        "license_report.md": out_dir / "license_report.md",
        "day2_attribution.md": out_dir / "day2_attribution.md",
        "schedule_report.md": out_dir / "schedule_report.md",
        "counterfactual.md": out_dir / "counterfactual.md",
    }
    paths["license_report.md"].write_text(license_markdown(ds, rows, vmap), encoding="utf-8")
    paths["day2_attribution.md"].write_text(attribution_markdown(attr), encoding="utf-8")
    paths["schedule_report.md"].write_text(schedule_markdown(ds, vmap), encoding="utf-8")
    paths["counterfactual.md"].write_text(counterfactual_markdown(findings), encoding="utf-8")

    machine = {
        "licenses": [asdict(r) for r in rows],
        "day2_attribution": json.loads(json.dumps(attr, default=_json_default, ensure_ascii=False)),
        "counterfactual": findings,
        "bookings": [
            {"booking": asdict(item["booking"]),
             "decision": item["decision"],
             "notes": item["notes"]}
            for item in reconcile_bookings(ds, vmap)
        ],
    }
    paths["results.json"] = out_dir / "results.json"
    paths["results.json"].write_text(
        json.dumps(machine, default=_json_default, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return paths
