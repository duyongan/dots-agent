"""PyInstaller 入口：等价于 `python -m dots`。"""

import sys

# 冻结后 Windows 管道默认按 GBK 编码，doctor 里的 ✓/✗ 会直接抛
# UnicodeEncodeError。控制台不受影响（Windows 控制台走 UTF-16 API）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from dots.cli import main

raise SystemExit(main())
