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
import json
import shutil
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dots import bugs

# 一次读太多会把一轮 tick 拖成批处理;桥那边 backlog 很大时慢慢追。
DEFAULT_READ_LIMIT = 200

_GROUP_KEYS = ("group", "room", "room_name", "roomName", "chat", "chat_name", "群名")
_SENDER_KEYS = ("sender", "from", "name", "user", "nickname", "发送人")
_TEXT_KEYS = ("text", "content", "msg", "message", "body", "内容")
_TS_KEYS = ("ts", "timestamp", "time", "date", "发送时间")
_ID_KEYS = ("msg_id", "id", "message_id", "msgId")


@dataclass
class Message:
    group: str
    sender: str
    text: str
    ts: float = field(default_factory=time.time)
    msg_id: str = ""

    @property
    def key(self) -> str:
        """幂等键。桥给了 msg_id 就用它,没给就按内容算 —— 同一条转发两次不会重复处理。"""
        if self.msg_id:
            return f"msg:{self.msg_id}"
        basis = f"{self.group}|{self.sender}|{int(self.ts)}|{self.text}"
        return "sha1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "group": self.group, "sender": self.sender, "text": self.text,
            "ts": self.ts, "msg_id": self.msg_id,
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
           msg_id: str = "") -> Path:
    """给桥和 CLI 用:往 spool 追加一条。整行一次写完,别让读侧看到半行。"""
    p = ensure_spool(cfg)
    msg = Message(group=group, sender=sender, text=text,
                  ts=float(ts) if ts else time.time(), msg_id=msg_id)
    line = json.dumps(msg.as_dict(), ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8", newline="") as f:
        f.write(line)
        f.flush()
    return p


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
    shutil.rmtree(root, ignore_errors=True)
    print("wechat: ok")


if __name__ == "__main__":
    demo()
