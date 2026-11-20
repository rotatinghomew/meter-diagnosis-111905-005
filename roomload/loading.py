"""读取 fixtures 下的合成 CSV 源记录。

约定（见 DATA.md）：
- UTF-8 CSV，首行字段名；空值不声明事实，不等于零；时段左闭右开。
- observations / events 的 supersedes 指向被替代的旧记录；收到新记录后旧记录作废。
- 转态事件只能归入符合时钟的候选 block。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


def parse_hhmm(value: str) -> int:
    """墙上时钟 HH:MM -> 当日分钟数。"""
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def fmt_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class Room:
    id: str
    size: str
    neighbors: tuple[str, ...]
    door: str
    point: str
    shared_point: str
    staff_skill: str


@dataclass(frozen=True)
class Trial:
    id: str
    day: int
    start: int
    end: int
    a_on: bool
    b_on: bool
    door: str  # open / closed —— 记档安排，门控事实以 events 为准
    equipment_on: bool
    rotation: str


@dataclass(frozen=True)
class Segment:
    """一段 5 分钟区间内维持的点位读数。"""
    id: str
    block: str
    point: str
    start: int
    end: int
    level: float
    received: str
    supersedes: str = ""


@dataclass(frozen=True)
class Event:
    id: str
    day: int
    kind: str
    target: str
    start: int
    end: int
    candidates: tuple[str, ...]
    received: str
    supersedes: str = ""


@dataclass(frozen=True)
class Threshold:
    id: str
    points: tuple[str, ...]
    cutoff: float
    increase_cap: float
    min_paired_days: int


@dataclass(frozen=True)
class Booking:
    id: str
    room: str
    day: int
    start: int
    end: int
    group: str
    size_need: str
    kind: str  # signed / performed / offered / requested / declined
    prior: str = ""


@dataclass(frozen=True)
class StaffDuty:
    id: str
    day: int
    start: int
    end: int
    target: str
    resource: str
    passage: bool


@dataclass
class Dataset:
    rooms: dict[str, Room]
    trials: dict[str, Trial]
    segments: list[Segment]
    events: list[Event]
    thresholds: dict[str, Threshold]
    bookings: list[Booking]
    duties: list[StaffDuty]
    superseded_observations: set[str] = field(default_factory=set)
    superseded_events: set[str] = field(default_factory=set)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _apply_supersessions(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], set[str]]:
    dead = {r["supersedes"] for r in rows if r.get("supersedes")}
    alive = [r for r in rows if r["id"] not in dead]
    return alive, dead


def load(fixture_dir: str | Path) -> Dataset:
    fixture_dir = Path(fixture_dir)

    rooms: dict[str, Room] = {}
    for r in _rows(fixture_dir / "rooms.csv"):
        rooms[r["id"]] = Room(
            id=r["id"],
            size=r["size"],
            neighbors=tuple(r["neighbors"].split(";")) if r["neighbors"] else (),
            door=r["door"],
            point=r["point"],
            shared_point=r["shared_point"],
            staff_skill=r["staff_skill"],
        )

    trials: dict[str, Trial] = {}
    for r in _rows(fixture_dir / "trials.csv"):
        trials[r["id"]] = Trial(
            id=r["id"],
            day=int(r["day"]),
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            a_on=r["a_on"] == "1",
            b_on=r["b_on"] == "1",
            door=r["door"],
            equipment_on=r["equipment"] == "1",
            rotation=r["rotation"],
        )

    obs_rows, dead_obs = _apply_supersessions(_rows(fixture_dir / "observations.csv"))
    segments = [
        Segment(
            id=r["id"],
            block=r["block"],
            point=r["point"],
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            level=float(r["level"]),
            received=r["received"],
            supersedes=r["supersedes"],
        )
        for r in obs_rows
    ]

    ev_rows, dead_ev = _apply_supersessions(_rows(fixture_dir / "events.csv"))
    events = [
        Event(
            id=r["id"],
            day=int(r["day"]),
            kind=r["kind"],
            target=r["target"],
            start=parse_hhmm(r["from"]),
            end=parse_hhmm(r["to"]),
            candidates=tuple(r["candidate"].split(";")) if r["candidate"] else (),
            received=r["received"],
            supersedes=r["supersedes"],
        )
        for r in ev_rows
    ]

    thresholds: dict[str, Threshold] = {}
    for r in _rows(fixture_dir / "thresholds.csv"):
        thresholds[r["id"]] = Threshold(
            id=r["id"],
            points=tuple(r["points"].split(";")),
            cutoff=float(r["cutoff"]),
            increase_cap=float(r["increase_cap"]),
            min_paired_days=int(r["min_paired_days"]),
        )

    bookings = [
        Booking(
            id=r["id"],
            room=r["room"],
            day=int(r["day"]),
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            group=r["group"],
            size_need=r["type"],
            kind=r["kind"],
            prior=r["prior"],
        )
        for r in _rows(fixture_dir / "bookings.csv")
    ]

    duties = [
        StaffDuty(
            id=r["id"],
            day=int(r["day"]),
            start=parse_hhmm(r["start"]),
            end=parse_hhmm(r["end"]),
            target=r["target"],
            resource=r["resource"],
            passage=r["passage"] == "yes",
        )
        for r in _rows(fixture_dir / "staff.csv")
    ]

    return Dataset(
        rooms=rooms,
        trials=trials,
        segments=segments,
        events=events,
        thresholds=thresholds,
        bookings=bookings,
        duties=duties,
        superseded_observations=dead_obs,
        superseded_events=dead_ev,
    )
