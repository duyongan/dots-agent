"""PyInstaller 入口。dots.exe 走命令行,dots-gui.exe 进图形界面。

无控制台的那个 exe 里 sys.stdout 是 None,print 会炸 —— 所以先把它接到
安装目录的日志文件。窗口版启动失败时再弹个消息框,不能静默消失。
"""

import os
import sys
import traceback
from pathlib import Path


def _is_gui() -> bool:
    return Path(sys.argv[0]).stem.lower().startswith("dots-gui")


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


if sys.stdout is None:
    try:
        _log = open(_app_dir() / "dots-gui.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log
    except OSError:
        pass

# 冻结后 Windows 管道默认按 GBK 编码,doctor 里的 ✓/✗ 会抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# dots-gui.exe 双击启动(没有参数)或只带 --port/--no-browser 时,等价于 dots gui。
if _is_gui() and (len(sys.argv) == 1 or sys.argv[1].startswith("--")):
    sys.argv.insert(1, "gui")

try:
    from dots.cli import main

    raise SystemExit(main())
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    if _is_gui():
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0, traceback.format_exc()[-2000:], "dots 启动失败", 0x10
            )
        except Exception:
            pass
    raise SystemExit(1)
