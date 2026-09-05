#!/usr/bin/env python
"""免安装入口：在任意目录下都能运行。

    python <仓库路径>\\run.py audit <档案路径>

与 `python -m qaudit.cli` 等价，但不要求当前目录在仓库下。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qaudit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
