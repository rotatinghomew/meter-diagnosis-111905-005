#!/usr/bin/env python3
"""报告生成入口（隔离模式安全）。

用法：python3 -I run_analysis.py [fixtures目录] [报告输出目录]
脚本目录在解释器启动时已进入 sys.path，-I 下也能导入本仓库的 roomload 包。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roomload.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
