"""微信群监控:消息从 spool 进来,dots 只读不登录。

为什么不直接连微信:登录、风控、扫码、UI 自动化都是会坏的东西,而它们是**桥**的问题,
不是 agent 的问题。所以这里定一个很窄的契约:

    workspace/wechat/inbox.jsonl   一行一条 JSON

桥可以是 Wechaty、wechatferry、企业微信机器人转发、桌面 RPA,甚至是人工粘贴导出的
聊天记录 —— 只要往这个文件追加。dots 按字节偏移读增量,读到哪记在 meta 里。
桥坏了换桥,这边一行不用改。

一条消息最少要有 text,其它字段能认的别名都认:

    {"group": "项目A-线上问题", "sender": "张三", "text": "导出报 500", "ts": 1731000000}
    {"room_name": "...", "from": "...", "content": "...", "msg_id": "..."}

桥不方便写文件时,用内置的本机 webhook:`dots wechat hook --port 8790`,
POST /wechat 的 JSON 原样追加进 spool。只听 127.0.0.1。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dots import bugs

# 一次读太多会把一轮 tick 拖成批处理;桥那边 backlog 很大时慢慢追。
DEFAULT_READ_LIMIT = 200

_GROUP_KEYS = ("group", "room", "room_name", "roomName", "chat", "chat_name", "群名")
_SENDER_KEYS = ("sender", "from", "name", "user", "nickname", "发送人")
_TEXT_KEYS = ("text", "content", "msg", "message", "body", "内容")
_TS_KEYS = ("ts", "timestamp", "time", "date", "发送时间", "消息发送时间")
_ID_KEYS = ("msg_id", "id", "message_id", "msgId")
_TYPE_KEYS = ("msg_type", "type", "消息类型")


@dataclass
class Message:
    group: str
    sender: str
    text: str
    ts: float = field(default_factory=time.time)
    msg_id: str = ""
    msg_type: str = ""
    # bridge = 外部桥写进来的;pyweixin = dots 自己采的
    via: str = "bridge"

    @property
    def key(self) -> str:
        """幂等键。桥/RPA 给了 msg_id 就用它,没给就按内容算 —— 同一条转发两次不会重复处理。"""
        if self.msg_id:
            return f"msg:{self.msg_id}"
        basis = f"{self.group}|{self.sender}|{int(self.ts)}|{self.text}"
        return "sha1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "group": self.group, "sender": self.sender, "text": self.text,
            "ts": self.ts, "msg_id": self.msg_id, "msg_type": self.msg_type,
            "via": self.via,
        }


def spool_path(cfg) -> Path:
    return cfg.workspace / cfg.sources.wechat.spool


def ensure_spool(cfg) -> Path:
    p = spool_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    return p


def _first(raw: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def parse(raw) -> Message | None:
    """一行 JSON -> Message。缺正文的丢弃。"""
    if isinstance(raw, str):
        return Message(group="", sender="", text=raw.strip())
    if not isinstance(raw, dict):
        return None
    text = _first(raw, _TEXT_KEYS).strip()
    if not text:
        return None
    ts = time.time()
    raw_ts = raw.get("ts") or raw.get("timestamp") or raw.get("time")
    try:
        ts = float(raw_ts) if raw_ts not in (None, "") else ts
    except (TypeError, ValueError):
        ts = ts
    if ts > 1e11:        # 毫秒时间戳
        ts = ts / 1000.0
    return Message(
        group=_first(raw, _GROUP_KEYS),
        sender=_first(raw, _SENDER_KEYS),
        text=text,
        ts=ts,
        msg_id=_first(raw, _ID_KEYS),
        msg_type=_first(raw, _TYPE_KEYS),
        via=str(raw.get("via") or "bridge"),
    )


def read_new(cfg, cursor: str, limit: int = DEFAULT_READ_LIMIT) -> tuple[list[Message], str]:
    """按字节偏移读增量。返回 (消息, 新偏移)。

    文件变短了(桥轮转/重建)就从头读 —— 事件表的 UNIQUE 会挡住重复的。
    偏移以「读完的那一行为界」推进,半行永远留给下一次,免得读到半截 JSON。
    """
    p = spool_path(cfg)
    if not p.exists():
        return [], ""
    size = p.stat().st_size
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        offset = 0
    if offset > size:
        offset = 0

    out: list[Message] = []
    new_offset = offset
    with p.open("rb") as f:
        f.seek(offset)
        while len(out) < limit:
            line = f.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # 桥还在写这一行。不推进偏移,下次从这儿重新读。
                break
            new_offset = f.tell()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = parse(json.loads(text))
            except ValueError:
                msg = parse(text)      # 不是 JSON 就当纯文本一行,别把消息丢了
            if msg is not None:
                out.append(msg)
    return out, (str(new_offset) if new_offset > offset else "")


def groups_allowed(cfg, message: Message) -> bool:
    wanted = [str(g) for g in (cfg.sources.wechat.groups or [])]
    if not wanted:
        return True
    return message.group in wanted


def append(cfg, *, group: str, sender: str, text: str, ts: float | None = None,
           msg_id: str = "", msg_type: str = "", via: str = "bridge") -> Path:
    """给桥、CLI 和 pyweixin 采集器用:往 spool 追加一条。

    整行一次写完,别让读侧看到半行 —— read_new 靠「必须换行结束」判断一行是否完整。
    """
    p = ensure_spool(cfg)
    msg = Message(group=group, sender=sender, text=text,
                  ts=float(ts) if ts else time.time(), msg_id=msg_id,
                  msg_type=msg_type, via=via)
    line = json.dumps(msg.as_dict(), ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8", newline="") as f:
        f.write(line)
        f.flush()
    return p


# ------------------------------------------------------- pyweixin 直接采集

# 同一条消息在一次进程生命周期内只写一次 spool。
# 重启后可能再写一遍,靠 events 表的 UNIQUE 兜住,不会重复开目标。
_SEEN: set[str] = set()
_MAX_SEEN = 4000

_FULL_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})")
_SHORT_DATE = re.compile(r"^(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})$")
_HM = re.compile(r"^(\d{1,2}):(\d{2})$")
_WEEKDAYS = {"星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3,
             "星期五": 4, "星期六": 5, "星期日": 6, "星期天": 6}


def backend(cfg) -> str:
    return str(getattr(cfg.sources.wechat, "backend", "spool") or "spool")


def _load_pyweixin(cfg):
    """把 pyweixin 导进来。library_path 优先于当前环境里已安装的同名包。

    这一步只在真用 RPA 采集时发生 —— 模块顶层不导入,没装 pywin32 的人
    照样能跑 dots 的其它部分。
    """
    lib = (getattr(cfg.sources.wechat, "library_path", "") or "").strip()
    if lib:
        path = Path(lib).expanduser()
        if not path.exists():
            raise RuntimeError(f"library_path 不存在:{path}")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        return importlib.import_module("pyweixin")
    except ImportError as e:
        raise RuntimeError(
            "没找到 pyweixin。把 [sources.wechat] library_path 指到 pywechat 的 src 目录"
            "(例如 C:/Users/you/Documents/project/pywechat/src),"
            "或者先把 pyweixin 装进当前 Python 环境。"
        ) from e


def parse_cn_time(text: str, *, now: float | None = None) -> float:
    """解析微信聊天记录里的时间戳。解析不出来返回 0.0。

    格式来自 pyweixin 的 Chathistory_Timestamp_pattern:
    2026年11月08日 14:30 / 11月08日 14:30 / 昨天 14:30 / 星期一 14:30 / 14:30
    只有时间没有日期时,按「最近一次」算:比现在还晚一小时以上就当成昨天。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    base = datetime.fromtimestamp(now) if now else datetime.now()

    def at(hour: int, minute: int, day: datetime) -> float:
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()

    m = _FULL_DATE.search(raw)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        try:
            return at(h, mi, base.replace(year=y, month=mo, day=d))
        except ValueError:
            return 0.0

    m = _SHORT_DATE.search(raw)
    if m:
        mo, d, h, mi = (int(x) for x in m.groups())
        try:
            dt = at(h, mi, base.replace(month=mo, day=d))
        except ValueError:
            return 0.0
        # 12 月看到 1 月的时间戳 -> 是去年的消息
        if dt > base.timestamp() + 86400:
            dt = at(h, mi, base.replace(year=base.year - 1, month=mo, day=d))
        return dt

    if raw.startswith("昨天"):
        m = _HM.search(raw.replace("昨天", "").strip())
        if m:
            return at(int(m.group(1)), int(m.group(2)), base - timedelta(days=1))
        return 0.0

    for name, weekday in _WEEKDAYS.items():
        if raw.startswith(name):
            m = _HM.search(raw[len(name):].strip())
            if not m:
                return 0.0
            delta = (base.weekday() - weekday) % 7
            return at(int(m.group(1)), int(m.group(2)), base - timedelta(days=delta))

    if raw.startswith("今天"):
        raw = raw[2:].strip()
    m = _HM.search(raw)
    if m:
        ts = at(int(m.group(1)), int(m.group(2)), base)
        if ts > base.timestamp() + 3600:      # 23:00 看到 01:00,那是昨天的
            ts -= 86400
        return ts
    return 0.0


def messages_from_pyweixin(chat: str, records, *, now: float | None = None) -> list[Message]:
    """pyweixin 的聊天记录 -> Message。纯函数,测试直接喂假数据。

    pyweixin 记录长这样(键就是中文,别改):
        {'消息发送人','消息内容','消息类型','消息发送时间'}
    没有消息 ID,所以按「会话+发送人+时间+内容」算一个稳定指纹当 msg_id:
    同一条消息重复读到不会重复入账。
    """
    out: list[Message] = []
    fallback = float(now) if now else time.time()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        text = str(rec.get("消息内容") or rec.get("内容") or "").strip()
        if not text:
            continue
        sender = str(rec.get("消息发送人") or rec.get("发送人") or "").strip()
        kind = str(rec.get("消息类型") or "").strip()
        ts = parse_cn_time(str(rec.get("消息发送时间") or ""), now=fallback) or fallback
        basis = f"{chat}|{sender}|{int(ts)}|{text}"
        msg_id = "wx" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:18]
        out.append(Message(group=chat, sender=sender, text=text, ts=ts,
                           msg_id=msg_id, msg_type=kind, via="pyweixin"))
    return out


def _remember(key: str) -> None:
    _SEEN.add(key)
    if len(_SEEN) > _MAX_SEEN:      # 别让长驻进程攒成内存泄漏;丢了靠 events 兜底
        _SEEN.clear()


def collect(cfg, *, chats=None, now: float | None = None) -> dict:
    """扫一遍未读会话,把配置里的群消息拉下来写进 spool。返回一份统计。

    只做采集,不做 BUG 判定 —— 判定/去重/派活全在 worker,和外部桥走同一条路。
    任何异常都变成 {"error": ...}:RPA 会碰到窗口被关、控件找不到,不能让 tick 崩。

    配了 python_exe 就走子进程(dots 和 pyweixin 的依赖经常不在同一个环境);
    没配就在当前进程里直接 import,省一次启动。
    """
    if os.name != "nt":
        return {"error": "pyweixin 只能跑在 Windows 上"}
    sub = cfg.sources.wechat
    python_exe = (getattr(sub, "python_exe", "") or "").strip()
    if python_exe:
        payload = _run_script(cfg, python_exe, chats=chats, check=False)
        if payload.get("error"):
            return {"error": payload["error"]}
        return _ingest_payload(cfg, payload, now=now)
    return _collect_inprocess(cfg, chats=chats, now=now)


def _collect_inprocess(cfg, *, chats=None, now=None) -> dict:
    sub = cfg.sources.wechat
    try:
        wx = _load_pyweixin(cfg)
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        if not wx.Tools.is_weixin_running():
            return {"error": "微信没在运行(找不到 Weixin.exe),先启动并登录"}
    except Exception as e:                      # noqa: BLE001 - RPA 的异常类型很杂
        return {"error": f"检测微信状态失败:{type(e).__name__}: {e}"}

    utils_mod = importlib.import_module("pyweixin.utils")
    try:
        unread = utils_mod.scan_for_new_messages(close_weixin=False, is_maximize=False)
    except Exception as e:                      # noqa: BLE001
        return {"error": f"扫描未读会话失败:{type(e).__name__}: {e}"}
    if not isinstance(unread, dict):
        unread = {}

    groups = [str(g) for g in (sub.groups or [])]
    max_pull = max(1, int(getattr(sub, "max_pull", 20) or 20))
    if chats:
        targets = {str(c): int(unread.get(str(c)) or max_pull) for c in chats}
    else:
        targets = {str(k): int(v) for k, v in unread.items()
                   if not groups or str(k) in groups}

    payload: dict = {"unread": unread, "chats": {}, "errors": {}}
    for chat, count in targets.items():
        number = max(1, min(int(count), max_pull))
        try:
            records = wx.Messages.dump_chat_history(
                friend=chat, number=number, close_weixin=False, is_maximize=False,
            )
        except Exception as e:                  # noqa: BLE001
            payload["errors"][chat] = f"{type(e).__name__}: {e}"
            continue
        if not isinstance(records, list):
            payload["errors"][chat] = "没有拿到聊天记录"
            continue
        payload["chats"][chat] = [r for r in records if isinstance(r, dict)]
    return _ingest_payload(cfg, payload, now=now)


def _run_script(cfg, python_exe: str, *, chats=None, check: bool) -> dict:
    """用指定解释器跑 rpa_collector.py,把结果 JSON 读回来。

    结果写在文件里而不是 stdout:pyweixin 自己会 print/warn,stdout 不干净。
    """
    sub = cfg.sources.wechat
    script = Path(__file__).with_name("rpa_collector.py")
    out = cfg.workspace / "wechat" / (".check.json" if check else ".collector.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.unlink()
    except OSError:
        pass

    cmd = [python_exe, str(script), "--out", str(out)]
    lib = (getattr(sub, "library_path", "") or "").strip()
    if lib:
        cmd += ["--library-path", lib]
    if check:
        cmd.append("--check")
    else:
        cmd += ["--max-pull", str(max(1, int(getattr(sub, "max_pull", 20) or 20)))]
        groups = [str(g) for g in (sub.groups or [])]
        if groups:
            cmd += ["--groups", ",".join(groups)]
        if chats:
            cmd += ["--chats", ",".join(str(c) for c in chats)]

    timeout = int(getattr(sub, "collect_timeout", 240) or 240)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return {"error": f"python_exe 不存在:{python_exe}"}
    except subprocess.TimeoutExpired:
        return {"error": f"采集子进程超时({timeout}s),检查是不是弹了窗口/扫码框"}
    except OSError as e:
        return {"error": f"起采集子进程失败:{type(e).__name__}: {e}"}

    if not out.exists():
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
        return {"error": f"采集脚本没写出结果(exit={proc.returncode}):{tail or '无输出'}"}
    try:
        payload = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"采集结果读不回来:{type(e).__name__}: {e}"}
    finally:
        if not check:
            try:
                out.unlink()
            except OSError:
                pass
    return payload if isinstance(payload, dict) else {"error": "采集结果格式不对"}


def _ingest_payload(cfg, payload: dict, *, now=None) -> dict:
    """把采集结果(会话 -> 原始聊天记录)归一化并写 spool。

    子进程和内进程共用这一段,保证两条路的指纹、去重、时间解析完全一致。
    """
    chats_data = payload.get("chats") or {}
    errors = payload.get("errors") or {}
    unread = payload.get("unread") or {}
    if not isinstance(chats_data, dict):
        chats_data = {}

    written = 0
    skipped = 0
    detail: dict[str, str] = {}
    for chat, records in chats_data.items():
        got = messages_from_pyweixin(str(chat), records, now=now)
        for msg in got:
            if msg.key in _SEEN:
                skipped += 1
                continue
            append(cfg, group=msg.group, sender=msg.sender, text=msg.text,
                   ts=msg.ts, msg_id=msg.msg_id, msg_type=msg.msg_type,
                   via=msg.via)
            _remember(msg.key)
            written += 1
        count = _safe_int(unread.get(chat))
        detail[str(chat)] = f"{len(got)} 条" + (f"(未读 {count})" if count else "")

    for chat, err in (errors or {}).items():
        detail[f"{chat}"] = f"失败:{err}"

    return {
        "chats": len(chats_data) + len(errors or {}),
        "messages": written,
        "skipped": skipped,
        "unread_total": sum(_safe_int(v) for v in unread.values()),
        "detail": detail,
    }


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_env(cfg) -> tuple[bool, str]:
    """体检:pyweixin 能用吗、微信在跑吗、UI 树可见吗。"""
    if os.name != "nt":
        return False, "pyweixin 只在 Windows 上可用"
    python_exe = (getattr(cfg.sources.wechat, "python_exe", "") or "").strip()
    if python_exe:
        payload = _run_script(cfg, python_exe, check=True)
        if payload.get("error"):
            return False, str(payload["error"])
        return bool(payload.get("ok")), str(payload.get("detail") or "")
    return _check_inprocess(cfg)


def _check_inprocess(cfg) -> tuple[bool, str]:
    try:
        wx = _load_pyweixin(cfg)
    except RuntimeError as e:
        return False, str(e)
    try:
        if not wx.Tools.is_weixin_running():
            return False, "微信没在运行,先启动并登录"
    except Exception as e:                      # noqa: BLE001
        return False, f"检测微信状态失败:{type(e).__name__}: {e}"
    try:
        hwnd = wx.Tools.get_weixin_hwnd()
    except Exception as e:                      # noqa: BLE001
        return False, f"找不到微信窗口:{type(e).__name__}: {e}"
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


# ------------------------------------------------------------------- webhook

class _Handler(BaseHTTPRequestHandler):
    server_version = "dots-wechat/1.0"

    def log_message(self, fmt, *args):     # 别往 stderr 刷访问日志
        pass

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        cfg = self.server.cfg
        want = getattr(cfg.sources.wechat, "token", "") or ""
        if not want:
            return True
        return self.headers.get("X-Dots-Token", "") == want

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "spool": str(spool_path(self.server.cfg))})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/wechat", "/"):
            self._reply(404, {"error": "not found"})
            return
        # 先把请求体读完再判鉴权。反过来会在 401 时留下没读的 body,
        # 连接被 RST,客户端看到的是 ConnectionAborted 而不是 401(踩过)。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4 * 1024 * 1024:
            self._reply(413, {"error": "too large"})
            return
        try:
            raw = self.rfile.read(length) if length else b"{}"
        except OSError:
            self._reply(400, {"error": "bad body"})
            return

        if not self._allowed():
            self._reply(401, {"error": "bad token"})
            return
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            self._reply(400, {"error": f"bad json: {e}"})
            return
        items = data if isinstance(data, list) else [data]
        written = 0
        for item in items:
            msg = parse(item)
            if msg is None:
                continue
            append(self.server.cfg, group=msg.group, sender=msg.sender,
                   text=msg.text, ts=msg.ts, msg_id=msg.msg_id)
            written += 1
        self._reply(200, {"ok": True, "written": written})


def serve(cfg, *, port: int = 8790, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """起本机 webhook。返回 server,调用方负责 serve_forever / shutdown。

    只听 127.0.0.1:这是给同一台机器上的桥用的,不是公网接口。
    写死 host 参数只为了测试能传 127.0.0.1,别改成 0.0.0.0。
    """
    ensure_spool(cfg)
    httpd = ThreadingHTTPServer((host, int(port)), _Handler)
    httpd.cfg = cfg
    return httpd


def serve_forever(cfg, *, port: int = 8790) -> None:
    httpd = serve(cfg, port=port)
    print(f"[dot] 微信群 webhook 起来了:http://127.0.0.1:{port}/wechat"
          f"  spool={spool_path(cfg)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dot] webhook 停了")
    finally:
        httpd.server_close()


def demo() -> None:
    """纯逻辑自检:解析别名、增量读、半行、幂等键。"""
    root = Path.cwd() / f".dots-wechat-demo-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    cfg = type("C", (), {"workspace": root, "sources": type("S", (), {
        "wechat": type("W", (), {
            "enabled": True, "spool": "wechat/inbox.jsonl", "groups": [],
            "keywords": [], "interval": 60, "max_batch": 10, "token": "",
        })(),
    })()})()

    assert parse({"room_name": "项目A", "from": "张三", "content": "报错了",
                  "msg_id": "m1"}).key == "msg:m1"
    assert parse({"text": "hi"}).group == ""
    assert parse({"no_text": 1}) is None

    append(cfg, group="项目A", sender="张三", text="导出报 500")
    append(cfg, group="项目B", sender="李四", text="样式不对")

    msgs, cursor = read_new(cfg, "")
    assert len(msgs) == 2, msgs
    assert msgs[0].group == "项目A" and msgs[0].text == "导出报 500"
    msgs2, cursor2 = read_new(cfg, cursor)
    assert msgs2 == [] and cursor2 == "", (msgs2, cursor2)

    # 半行:桥写到一半时不能被吃掉
    p = spool_path(cfg)
    with p.open("ab") as f:
        f.write(b'{"group":"A","text":"half')
    msgs3, cursor3 = read_new(cfg, cursor)
    assert msgs3 == [] and cursor3 == "", (msgs3, cursor3)
    with p.open("ab") as f:
        f.write(b' line"}\n')
    msgs4, cursor4 = read_new(cfg, cursor)
    assert len(msgs4) == 1 and msgs4[0].text == "half line", msgs4
    assert cursor4

    # 轮转:文件变短就从头读
    p.write_text("", encoding="utf-8")
    append(cfg, group="项目A", sender="张三", text="文件被重建了")
    msgs5, _ = read_new(cfg, cursor4)
    assert len(msgs5) == 1 and "重建" in msgs5[0].text, msgs5

    assert groups_allowed(cfg, msgs[1])
    cfg.sources.wechat.groups = ["项目A"]
    assert groups_allowed(cfg, msgs[0]) and not groups_allowed(cfg, msgs[1])

    assert bugs.should_report("导出报 500")

    # --- pyweixin 采集:时间戳解析 + 记录归一化(纯函数,不碰 UI)
    now = datetime(2026, 11, 8, 15, 0).timestamp()
    assert abs(parse_cn_time("2026年11月08日 14:30", now=now)
               - datetime(2026, 11, 8, 14, 30).timestamp()) < 1
    assert abs(parse_cn_time("昨天 09:05", now=now)
               - datetime(2026, 11, 7, 9, 5).timestamp()) < 1
    assert abs(parse_cn_time("星期一 10:00", now=now)
               - datetime(2026, 11, 2, 10, 0).timestamp()) < 1
    assert abs(parse_cn_time("14:00", now=now)
               - datetime(2026, 11, 8, 14, 0).timestamp()) < 1
    assert parse_cn_time("", now=now) == 0.0

    got = messages_from_pyweixin("项目A-线上问题", [{
        "消息发送人": "张三", "消息内容": "导出报 500",
        "消息类型": "文本", "消息发送时间": "2026年11月08日 14:30",
    }], now=now)
    assert len(got) == 1 and got[0].group == "项目A-线上问题"
    assert got[0].msg_id.startswith("wx") and got[0].via == "pyweixin"
    assert got[0].msg_type == "文本"
    assert messages_from_pyweixin("A", [{"消息内容": ""}]) == []

    shutil.rmtree(root, ignore_errors=True)
    print("wechat: ok")


if __name__ == "__main__":
    demo()
