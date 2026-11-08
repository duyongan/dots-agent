"""独立的微信群采集脚本。只依赖 pyweixin,不 import dots。

为什么单独一个脚本、还要能被别的解释器跑:
    dots 要 langchain,pyweixin 要 pywinauto/pyautogui,两套依赖经常不在同一个
    Python 环境里(打包版更是这样)。所以采集这半边不放进进程,交给
    `[sources.wechat] python_exe` 指定的解释器跑,结果写成 JSON 回传。
    顺带的好处:UI 自动化崩了也只崩子进程,不会带倒常驻 agent。

用法(由 dots 调用,也可以手工跑):
    <python_exe> rpa_collector.py --out result.json [--library-path DIR]
                 [--groups 群A,群B] [--chats 群A] [--max-pull 20]

输出文件形如:
    {"unread": {"群A": 3}, "chats": {"群A": [{"消息发送人": ..., ...}]},
     "errors": {"群B": "失败:..."}}
失败时:{"error": "人话原因"}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _write(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fail(out: str, message: str) -> int:
    _write(out, {"error": message})
    return 1


def _check_env(pyweixin) -> tuple[bool, str]:
    try:
        if not pyweixin.Tools.is_weixin_running():
            return False, "微信没在运行,先启动并登录"
        hwnd = pyweixin.Tools.get_weixin_hwnd()
    except Exception as e:                      # noqa: BLE001
        return False, f"检测微信状态失败:{type(e).__name__}: {e}"
    if not hwnd:
        return False, "找不到微信窗口(未启动或未登录)"
    try:
        from pywinauto import Desktop

        cls = Desktop(backend="uia").window(handle=hwnd).class_name()
    except Exception as e:                      # noqa: BLE001
        return False, f"检查 UI 可见性失败:{type(e).__name__}: {e}"
    if "mmui" not in (cls or ""):
        return False, "微信 UI 树不可见:开启 Windows 无障碍(讲述人)后再试"
    return True, "微信已登录且 UI 可见"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dots-wechat-collector")
    parser.add_argument("--out", required=True, help="结果 JSON 写到哪")
    parser.add_argument("--library-path", default="", help="pyweixin 源码目录")
    parser.add_argument("--groups", default="", help="只看这些群,逗号分隔")
    parser.add_argument("--chats", default="", help="强制采这些会话,逗号分隔")
    parser.add_argument("--max-pull", type=int, default=20)
    parser.add_argument("--check", action="store_true",
                        help="只检查环境(微信在跑吗、UI 可见吗),不拉消息")
    args = parser.parse_args(argv)

    if os.name != "nt":
        return _fail(args.out, "pyweixin 只能跑在 Windows 上")
    if args.library_path:
        lib = Path(args.library_path).expanduser()
        if not lib.exists():
            return _fail(args.out, f"library_path 不存在:{lib}")
        sys.path.insert(0, str(lib))

    try:
        import pyweixin
        from pyweixin import utils as wx_utils
    except Exception as e:                      # noqa: BLE001 - 缺依赖/版本不对都归这里
        return _fail(args.out, f"导入 pyweixin 失败:{type(e).__name__}: {e}")

    if args.check:
        ok, detail = _check_env(pyweixin)
        _write(args.out, {"ok": ok, "detail": detail})
        return 0 if ok else 1

    try:
        if not pyweixin.Tools.is_weixin_running():
            return _fail(args.out, "微信没在运行(找不到 Weixin.exe),先启动并登录")
    except Exception as e:                      # noqa: BLE001
        return _fail(args.out, f"检测微信状态失败:{type(e).__name__}: {e}")

    groups = [x.strip() for x in args.groups.split(",") if x.strip()]
    forced = [x.strip() for x in args.chats.split(",") if x.strip()]
    max_pull = max(1, int(args.max_pull or 20))

    try:
        unread = wx_utils.scan_for_new_messages(close_weixin=False, is_maximize=False)
    except Exception as e:                      # noqa: BLE001
        return _fail(args.out, f"扫描未读会话失败:{type(e).__name__}: {e}")
    if not isinstance(unread, dict):
        unread = {}

    if forced:
        targets = {name: int(unread.get(name) or max_pull) for name in forced}
    else:
        targets = {str(k): int(v) for k, v in unread.items()
                   if not groups or str(k) in groups}

    payload: dict = {"unread": unread, "chats": {}, "errors": {}}
    for chat, count in targets.items():
        number = max(1, min(int(count), max_pull))
        try:
            records = pyweixin.Messages.dump_chat_history(
                friend=chat, number=number, close_weixin=False, is_maximize=False,
            )
        except Exception as e:                  # noqa: BLE001
            payload["errors"][chat] = f"{type(e).__name__}: {e}"
            continue
        if not isinstance(records, list):
            payload["errors"][chat] = "没有拿到聊天记录"
            continue
        payload["chats"][chat] = [r for r in records if isinstance(r, dict)]

    if not payload["chats"] and payload["errors"]:
        payload["error"] = "所有会话都采集失败,先跑 dots wechat check"
    _write(args.out, payload)
    return 0 if payload["chats"] or not targets else 1


if __name__ == "__main__":
    raise SystemExit(main())
