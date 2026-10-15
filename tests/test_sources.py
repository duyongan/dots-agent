"""事件源的真测试。

IMAP 和 ical 原来从没连过真服务器。这里不去连真服务器,而是:
  - 起一个真的 TLS IMAP 服务器(自签证书),让真实的 imaplib 走完整协议
  - 起一个真的 HTTP 服务器喂 .ics,让真实的 urlopen 走完整链路

测的是真代码路径:登录、UID 增量、RFC822 取值、MIME 解码、RRULE 展开。
不是 mock,是本地假服务。

    python -m pytest tests/test_sources.py -q
    python -m tests.test_sources
"""

from __future__ import annotations

import datetime as dt
import email
import os
import socket
import socketserver
import ssl
import tempfile
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from types import SimpleNamespace

from dot import sources


# ===================================================== 自签证书 + 假 IMAP 服务

def _make_cert(directory: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = os.path.join(directory, "cert.pem")
    keyfile = os.path.join(directory, "key.pem")
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(keyfile, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return certfile, keyfile


class _FakeIMAP(socketserver.StreamRequestHandler):
    """够 imaplib 走完 LOGIN / SELECT / UID SEARCH / UID FETCH / LOGOUT 就行。"""

    def handle(self):
        self._send(b"* OK fake IMAP ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            tag, _, rest = text.partition(" ")
            tokens = rest.split()
            cmd = tokens[0].upper() if tokens else ""

            if cmd == "CAPABILITY":
                self._send(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                self._ok(tag, "CAPABILITY")
            elif cmd == "LOGIN":
                self._ok(tag, "LOGIN")
            elif cmd == "SELECT":
                self._send(f"* {len(self.server.messages)} EXISTS".encode())
                self._send(b"* OK [UIDVALIDITY 1] UIDs valid")
                self._ok(tag, "SELECT", "[READ-ONLY]")
            elif cmd == "UID":
                self._uid(tag, tokens)
            elif cmd == "NOOP":
                self._ok(tag, "NOOP")
            elif cmd == "LOGOUT":
                self._send(b"* BYE bye")
                self._ok(tag, "LOGOUT")
                return
            else:
                self._ok(tag, cmd or "UNKNOWN")

    def _uid(self, tag, tokens):
        verb = tokens[1].upper() if len(tokens) > 1 else ""
        if verb == "SEARCH":
            uids = sorted(self.server.messages)
            body = b" ".join(str(u).encode() for u in uids)
            self._send(b"* SEARCH " + body)
            self._ok(tag, "UID SEARCH")
        elif verb == "FETCH":
            uid = int(tokens[2])
            raw = self.server.messages.get(uid)
            if raw is None:
                self._ok(tag, "UID FETCH")
                return
            # 字面量形式:imaplib 看到 {N} 就先读 N 字节
            self.wfile.write(b"* 1 FETCH (RFC822 {%d}\r\n" % len(raw) + raw + b")\r\n")
            self._ok(tag, "UID FETCH")
        else:
            self._ok(tag, "UID")

    def _send(self, data: bytes):
        self.wfile.write(data + b"\r\n")

    def _ok(self, tag: str, name: str, extra: str = ""):
        parts = [tag, "OK", extra, name, "completed"]
        self._send(" ".join(p for p in parts if p).encode())


class FakeIMAPServer:
    def __init__(self, messages: dict[int, bytes]):
        self._tmp = tempfile.TemporaryDirectory()
        certfile, keyfile = _make_cert(self._tmp.name)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), _FakeIMAP)
        self.server.messages = messages
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()


# ============================================================ 假 HTTP 服务

class FakeHTTPServer:
    def __init__(self, body: bytes, content_type: str = "text/calendar"):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ================================================================ 测试用例

def test_mime_decoding() -> str:
    """RFC2047 编码头 + 中文 + multipart/alternative。"""
    m = EmailMessage()
    m["Subject"] = "周会改到周四"
    m["From"] = "Alice <alice@example.com>"
    m.set_content("纯文本正文")
    m.add_alternative(
        "<html><body><p>Hello <b>world</b></p><script>alert(1)</script></body></html>",
        subtype="html",
    )
    msg = email.message_from_bytes(m.as_bytes())

    assert sources._hdr(msg.get("Subject")) == "周会改到周四", sources._hdr(msg.get("Subject"))
    assert "alice@example.com" in sources._hdr(msg.get("From"))
    # 有 text/plain 就优先用它,不要 HTML
    assert sources._body(msg) == "纯文本正文", sources._body(msg)
    return "  MIME:编码主题解出来了,multipart 优先取 text/plain  ✓"


def test_html_only_fallback() -> str:
    """只有 HTML 的邮件也要能读,标签和 script 内容都得去掉。"""
    m = EmailMessage()
    m["Subject"] = "只有 HTML"
    m.set_content(
        "<html><body><h1>标题</h1><p>第一段</p><p>第二段</p>"
        "<script>var x = 1;</script><style>p{color:red}</style></body></html>",
        subtype="html",
    )
    body = sources._body(email.message_from_bytes(m.as_bytes()))

    assert "标题" in body and "第一段" in body and "第二段" in body, body
    assert "<" not in body and ">" not in body, f"标签没去干净: {body}"
    assert "var x" not in body, f"script 内容漏进来了: {body}"
    assert "color:red" not in body, f"style 内容漏进来了: {body}"
    return "  MIME:只有 HTML 时压成纯文本,标签/script/style 都去掉了  ✓"


def test_parse_dt_and_timezone() -> str:
    """TZID 要真的算进去,不能把上海时间当 UTC。"""
    utc = sources._parse_dt("20261015T090000Z", "")
    assert utc == dt.datetime(2026, 10, 15, 9, 0, tzinfo=dt.timezone.utc)

    shanghai = sources._parse_dt("20261015T090000", ";TZID=Asia/Shanghai")
    assert shanghai == dt.datetime(2026, 10, 15, 1, 0, tzinfo=dt.timezone.utc), shanghai

    date_only = sources._parse_dt("20261015", "")
    assert date_only == dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    assert sources._parse_dt("垃圾数据", "") is None

    # 混着 naive 和 aware 比较会直接抛异常,所以全都得是 aware
    assert sources._parse_dt("20261015T090000", "").tzinfo is not None
    return "  日历:时间解析带 TZID 偏移,全部归一成 aware UTC  ✓"


def test_rrule_expansion() -> str:
    """重复日程要展开成窗口内的每一次,不是只报第一个。"""
    block = "BEGIN:VEVENT\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\nSUMMARY:周会\r\nEND:VEVENT"
    base = dt.datetime(2026, 10, 5, 9, 0, tzinfo=dt.timezone.utc)   # 周一
    lo = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    hi = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    got = sources._occurrences(block, base, lo, hi)
    assert len(got) == 4, f"应该展开 4 次周一,得到 {len(got)}: {got}"
    assert all(d.weekday() == 0 for d in got), got

    # EXDATE 的排除由调用方做,单独测一下过滤逻辑
    exdates = {got[1]}
    kept = [d for d in got if d not in exdates]
    assert len(kept) == 3

    # 没有 RRULE 就是单次
    single = sources._occurrences("BEGIN:VEVENT\r\nEND:VEVENT", base, lo, hi)
    assert single == [base]
    # 单次落在窗口外就不报
    assert sources._occurrences("BEGIN:VEVENT\r\nEND:VEVENT", base, hi, hi + dt.timedelta(days=1)) == []

    # RRULE 写坏了不能把整件事吞掉,要退回单次
    broken = sources._occurrences(
        "BEGIN:VEVENT\r\nRRULE:FREQ=NOTAREALFREQ\r\nEND:VEVENT", base, lo, hi
    )
    assert broken == [base], broken
    return "  日历:RRULE 展开成多次、单次不重复、坏 RRULE 退回单次  ✓"


def test_ical_poll_over_http() -> str:
    """真起一个 HTTP 服务器喂 .ics,走完 urlopen 全链路。"""
    now = dt.datetime.now(dt.timezone.utc)

    def ics_stamp(delta_min: int) -> str:
        return (now + dt.timedelta(minutes=delta_min)).strftime("%Y%m%dT%H%M%SZ")

    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:soon@example.com\r\n"
        f"DTSTART:{ics_stamp(30)}\r\n"
        "SUMMARY:半小时后的会\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:far@example.com\r\n"
        f"DTSTART:{ics_stamp(600)}\r\n"
        "SUMMARY:十小时后的会\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")

    http = FakeHTTPServer(body)
    try:
        cfg = SimpleNamespace(
            sources=SimpleNamespace(
                ical=SimpleNamespace(url=f"http://127.0.0.1:{http.port}/cal.ics")
            )
        )
        events, cursor = sources.ical_poll(cfg, None, "")
        # 只有 120 分钟窗口内的那件事该报
        assert len(events) == 1, [e.payload for e in events]
        assert events[0].payload["summary"] == "半小时后的会", events[0].payload
        assert 25 <= events[0].payload["minutes_away"] <= 35, events[0].payload

        # 同一个窗口内再扫一遍:不能重复报
        events2, _ = sources.ical_poll(cfg, None, cursor)
        assert events2 == [], [e.payload for e in events2]
    finally:
        http.stop()
    return "  日历:HTTP 全链路拉到 .ics,窗口内触发、同一件事不重复  ✓"


def test_imap_poll_over_tls() -> str:
    """真起一个 TLS IMAP 服务器,让真 imaplib 走完登录和取值。"""
    now = dt.date.today().strftime("%a, %d %b %Y %H:%M:%S +0000")

    def raw(subject: str, body: str) -> bytes:
        m = EmailMessage()
        m["Subject"] = subject
        m["From"] = "Bob <bob@example.com>"
        m["Date"] = now
        m.set_content(body)
        return m.as_bytes()

    server = FakeIMAPServer({
        1: raw("第一封", "正文一"),
        2: raw("周会改到周四", "同意,改期没问题"),
        3: raw("[dot#ab12] 发邮件给 alice", "同意"),
    })
    try:
        cfg = SimpleNamespace(
            sources=SimpleNamespace(
                imap=SimpleNamespace(
                    host="127.0.0.1", port=server.port, user="me@example.com",
                    password="pw", folder="INBOX", verify_tls=False,
                )
            )
        )
        events, cursor = sources.imap_poll(cfg, None, "")

        assert len(events) == 3, [e.payload.get("subject") for e in events]
        assert [e.payload["subject"] for e in events] == [
            "第一封", "周会改到周四", "[dot#ab12] 发邮件给 alice",
        ], [e.payload["subject"] for e in events]
        assert events[1].payload["body"] == "同意,改期没问题", events[1].payload["body"]
        assert events[0].key == "uid:1" and events[2].key == "uid:3"
        assert cursor == "3", cursor

        # 增量:从游标继续,不能把已经见过的再拉一遍
        events2, cursor2 = sources.imap_poll(cfg, None, cursor)
        assert events2 == [], [e.payload.get("subject") for e in events2]
        assert cursor2 == "", f"没有新邮件时不该推进游标: {cursor2!r}"
    finally:
        server.stop()
    return "  IMAP:真 TLS 握手 + 登录 + UID 增量取值,游标不重复不空转  ✓"


def test_imap_unreachable_is_not_fatal() -> str:
    """IMAP 挂了必须只影响这一个源,不能把整轮 tick 带走。"""
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()

    conn = _quiet_conn()
    cfg = SimpleNamespace(
        sources=SimpleNamespace(
            fs=SimpleNamespace(enabled=False, paths=[], interval=60),
            imap=SimpleNamespace(
                enabled=True, host="127.0.0.1", port=port, user="u",
                password="p", folder="INBOX", verify_tls=False, interval=60,
            ),
            ical=SimpleNamespace(enabled=False, url="", interval=60),
        )
    )
    events = sources.poll_all(cfg, conn)
    assert events == [], events
    assert "source_error" in {r["kind"] for r in _audit_rows(conn)}, "源出错没记进审计"
    return "  IMAP:连不上只跳过这个源,错误记进 audit,不炸整轮  ✓"


# ------------------------------------------------------------------ 小工具

_TEMP_DBS: list[tuple[object, str]] = []


def _quiet_conn():
    from dot import db

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    db.init(conn)
    _TEMP_DBS.append((conn, path))
    return conn


def _cleanup_temp_dbs():
    """Windows 上不关连接就删不掉文件,所以先 close 再 unlink。"""
    while _TEMP_DBS:
        conn, path = _TEMP_DBS.pop()
        try:
            conn.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def _audit_rows(conn):
    return conn.execute("SELECT kind FROM audit").fetchall()


def run() -> str:
    import contextlib
    import io
    import sys

    cases = [
        test_mime_decoding,
        test_html_only_fallback,
        test_parse_dt_and_timezone,
        test_rrule_expansion,
        test_ical_poll_over_http,
        test_imap_poll_over_tls,
        test_imap_unreachable_is_not_fatal,
    ]
    notes = []
    try:
        for case in cases:
            # 被测代码会往 stderr 打诊断,自检时别混进来
            with contextlib.redirect_stderr(io.StringIO()):
                notes.append(case())
    finally:
        _cleanup_temp_dbs()
    return "\n".join(notes)


# ------------------------------------------------------------------- pytest

def test_all_sources():
    run()


if __name__ == "__main__":
    print(run())
