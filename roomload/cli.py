"""命令行入口：python3 -I -m roomload.cli <fixtures目录> <报告输出目录>"""
from __future__ import annotations

import sys
from pathlib import Path

from .loading import load
from .report import write_reports


def main(argv: list[str]) -> int:
    fixture_dir = Path(argv[1]) if len(argv) > 1 else Path("fixtures")
    out_dir = Path(argv[2]) if len(argv) > 2 else Path("reports")
    ds = load(fixture_dir)
    paths = write_reports(ds, out_dir)
    print(f"已读取 {len(ds.trials)} 个试运行 block、{len(ds.segments)} 条有效观测、"
          f"{len(ds.events)} 个生效事件")
    print(f"替代作废观测：{sorted(ds.superseded_observations)}；"
          f"替代作废事件：{sorted(ds.superseded_events)}")
    for name, path in paths.items():
        print(f"- {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
