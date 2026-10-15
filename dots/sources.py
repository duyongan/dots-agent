"""事件源。只读外部世界,绝不写数据库 —— 落库是 worker 的事。

加第四个源 = 加一个 poll 函数,别的文件一行不改。
签名统一:poll(cfg, conn, cursor) -> (events, new_cursor)
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import re
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen

from dateutil.rrule import rrulestr
from dateutil.tz import gettz

from dots import db


@dataclass
class Event:
    source: str
    key: str
    payload: dict = field(default_factory=dict)


def poll_all(cfg, conn) -> list[Event]:
    """每个源独立 try —— 一个源挂了不能拖垮整个 tick。"""
    out: list[Event] = []
    for name, poller, enabled in (
        ("fs", fs_poll, cfg.sources.fs.enabled),
        ("imap", imap_poll, cfg.sources.imap.enabled),
        ("ical", ical_poll, cfg.sources.ical.enabled),
    ):
        if not enabled:
            continue
        cursor = db.cursor_get(conn, name)
        try:
            events, new_cursor = poller(cfg, conn, cursor)
        except Exception as e:
            db.audit(conn, None, "source_error", f"{name}: {type(e).__name__}: {e}")
            print(f"[dot] {name} 源出错,跳过本轮: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        out.extend(events)
        if new_cursor:
            db.cursor_set(conn, name, new_cursor)
    return out


# ------------------------------------------------------------------ 文件变更

def fs_scan(cfg) -> list[tuple[str, str, float, int]]:
    """工作区里当前所有值得关注的文件 -> [(key, 相对路径, mtime, size)]。

    key 带 mtime_ns + size,所以「同一个文件改了」是新 key,
    「同一个文件原样再扔一次」是同一个 key —— 幂等靠 key,不靠 agent 聪明。

    worker 也用它来查「这一轮 agent 自己写了什么」,所以抽出来共用,
    别在两处各写一遍 key 的拼法,那种重复迟早会不一致。
    """
    out: list[tuple[str, str, float, int]] = []
    for rel in cfg.sources.fs.paths:
        root = cfg.workspace / rel
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            st = p.stat()
            if st.st_size == 0:
                continue  # ponytail: 空文件当没写完;真需要处理空文件就删这行
            relpath = p.relative_to(cfg.workspace).as_posix()
            out.append(
                (f"{relpath}:{st.st_mtime_ns}:{st.st_size}", relpath, st.st_mtime, st.st_size)
            )
    return out


def fs_poll(cfg, conn, cursor: str):
    """mtime 轮询。stdlib 就够,不值得为它引 watchdog。"""
    since = float(cursor) if cursor else 0.0
    latest = since
    events: list[Event] = []

    for key, relpath, mtime, size in fs_scan(cfg):
        if mtime <= since:
            continue
        events.append(
            Event(
                source="fs",
                key=key,
                payload={"path": relpath, "size": size, "mtime": mtime},
            )
        )
        latest = max(latest, mtime)

    return events, (str(latest) if latest > since else "")


# --------------------------------------------------------------------- 邮件

def imap_poll(cfg, conn, cursor: str):
    """按 UID 增量拉。一个 poller 干两件事:收事件 + 收审批回复。

    审批回复的识别不在这里做 —— 这里只负责把主题原样交出去,
    由 worker 判断是不是审批回信。源不关心业务。
    """
    sub = cfg.sources.imap
    if not (sub.host and sub.user and sub.password):
        return [], ""

    since = int(cursor) if cursor else 0
    events: list[Event] = []
    latest = since

    context = None
    if not getattr(sub, "verify_tls", True):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    imap = imaplib.IMAP4_SSL(sub.host, sub.port, timeout=30, ssl_context=context)
    try:
        imap.login(sub.user, sub.password)
        imap.select(sub.folder, readonly=True)
        typ, data = imap.uid("search", None, f"{since + 1}:*")
        if typ != "OK":
            return [], ""
        uids = [int(u) for u in (data[0] or b"").split()]
        for uid in uids:
            if uid <= since:
                continue
            typ, raw = imap.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                latest = max(latest, uid)
                continue
            msg = email.message_from_bytes(raw[0][1])
            events.append(
                Event(
                    source="imap",
                    key=f"uid:{uid}",
                    payload={
                        "uid": uid,
                        "subject": _hdr(msg.get("Subject")),
                        "from": _hdr(msg.get("From")),
                        "date": _hdr(msg.get("Date")),
                        "message_id": (msg.get("Message-ID") or "").strip(),
                        "body": _body(msg),
                    },
                )
            )
            latest = max(latest, uid)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return events, (str(latest) if latest > since else "")


def _hdr(value) -> str:
    """邮件头是编码雷区。解不出来就退化成 repr,别让整个源挂掉。"""
    if not value:
        return ""
    try:
        parts = decode_header(value)
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out).strip()
    except Exception:
        return str(value)


class _HtmlToText(HTMLParser):
    """把 HTML 压成能读的纯文本。

    ponytail: 不做布局还原,只保证「标签没了、块级元素有换行」。
    要保真的表格/列表排版就引 html2text,但模型能读现在这个。
    """
    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1
        elif tag in self._BREAK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1
        elif tag in self._BREAK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._depth:
            self.chunks.append(data)


def _html_to_text(html: str) -> str:
    p = _HtmlToText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    text = "".join(p.chunks)
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _body(msg, limit: int = 4000) -> str:
    """取正文:优先 text/plain,没有再退回 text/html 并压成纯文本。"""
    if msg.is_multipart():
        parts = [
            p for p in msg.walk()
            if not p.get("Content-Disposition")
        ]
        for wanted in ("text/plain", "text/html"):
            for part in parts:
                if part.get_content_type() != wanted:
                    continue
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                if wanted == "text/html":
                    text = _html_to_text(text)
                return text[:limit].strip()
        return ""
    raw = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    if msg.get_content_type() == "text/html":
        text = _html_to_text(text)
    return text[:limit].strip()


# --------------------------------------------------------------------- 日历

_VEVENT = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.S)
_UID = re.compile(r"^UID[^:]*:(.+)$", re.M)
# 带参数的那几个字段要连参数一起抓,不然 TZID 就丢了
_P_DTSTART = re.compile(r"^DTSTART([^:]*):(.+)$", re.M)
_P_EXDATE = re.compile(r"^EXDATE([^:]*):(.+)$", re.M)
_P_RRULE = re.compile(r"^RRULE:(.+)$", re.M)
_P_SUMMARY = re.compile(r"^SUMMARY:(.+)$", re.M)
_TZID = re.compile(r"TZID=([^;:]+)")


def ical_poll(cfg, conn, cursor: str, lookahead_min: int = 120):
    """日历。只在「某件事即将开始」时触发,而不是日历文件一变就触发。

    重复日程用 dateutil.rrule 展开 —— RRULE 有自己的语法,
    手写展开是经典的坑,不如把整段 RRULE 交给 rrulestr。

    ponytail: 不处理 RECURRENCE-ID(单次覆盖)。真被它坑到再加。
    """
    url = cfg.sources.ical.url
    if not url:
        return [], ""
    with urlopen(url, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    seen: set[str] = set()
    if cursor:
        try:
            seen = set(json.loads(cursor).get("seen", []))
        except Exception:
            seen = set()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=lookahead_min)
    events: list[Event] = []
    fresh: set[str] = set()

    for block in _VEVENT.findall(raw):
        m_start = _P_DTSTART.search(block)
        if not m_start:
            continue
        base = _parse_dt(m_start.group(2), m_start.group(1))
        if base is None:
            continue

        exdates = {
            dt
            for params, value in _P_EXDATE.findall(block)
            if (dt := _parse_dt(value, params)) is not None
        }
        uid = (_UID.search(block).group(1).strip() if _UID.search(block) else "") or digest
        m_sum = _P_SUMMARY.search(block)
        summary = m_sum.group(1).strip() if m_sum else "(无标题)"

        for start in _occurrences(block, base, now, horizon):
            if start in exdates:
                continue
            key = f"{uid}:{start.isoformat()}"
            fresh.add(key)
            if key in seen:
                continue
            events.append(
                Event(
                    source="ical",
                    key=key,
                    payload={
                        "summary": summary,
                        "starts_at": start.isoformat(),
                        "minutes_away": int((start - now).total_seconds() // 60),
                        "recurring": bool(_P_RRULE.search(block)),
                    },
                )
            )

    new_cursor = json.dumps({"digest": digest, "seen": sorted(fresh)})
    return events, (new_cursor if events else "")


def _occurrences(block: str, base: datetime, lo: datetime, hi: datetime) -> list[datetime]:
    """这一个 VEVENT 落在 [lo, hi] 窗口里的所有开始时间。"""
    m = _P_RRULE.search(block)
    if not m:
        return [base] if lo <= base <= hi else []
    try:
        rule = rrulestr(m.group(1).strip(), dtstart=base)
        return list(rule.between(lo, hi, inc=True))
    except Exception:
        # RRULE 解析不了(语法怪、或含我们不认的参数)就退回单次,
        # 至少别把这件事整个吞掉
        return [base] if lo <= base <= hi else []


def _parse_dt(value: str, params: str = "") -> datetime | None:
    """20261015T090000Z / 20261015T090000 / 20261015,可选 TZID=xxx 参数。

    全都归一成 aware UTC —— 混着 naive 和 aware 比较会直接抛异常。
    ponytail: 没有 TZID 的裸本地时间按 UTC 算,会偏一个时区。
    自用日历基本都带 Z 或 TZID,真偏了再加 local_tz 配置。
    """
    text = value.strip().split(",")[0].strip()
    naive = None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            naive = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        return None

    m = _TZID.search(params or "")
    if m:
        zone = gettz(m.group(1))
        if zone is not None:
            return naive.replace(tzinfo=zone).astimezone(timezone.utc)
    return naive.replace(tzinfo=timezone.utc)


def demo() -> None:
    """不联网也能跑的自检:造一个临时工作区,验证 fs 源的幂等。"""
    import tempfile
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "inbox").mkdir()
        cfg = SimpleNamespace(
            workspace=root,
            sources=SimpleNamespace(fs=SimpleNamespace(paths=["inbox"])),
        )
        f = root / "inbox" / "a.txt"
        f.write_text("hello", encoding="utf-8")

        events, cursor = fs_poll(cfg, None, "")
        assert len(events) == 1, events
        # cursor 交回去,同一文件不该再报一次 —— 这是 M1 的判据
        events2, cursor2 = fs_poll(cfg, None, cursor)
        assert events2 == [], f"幂等坏了: {events2}"
        # 改了内容 = 新事件
        f.write_text("hello world", encoding="utf-8")
        import os, time
        os.utime(f, (time.time() + 5, time.time() + 5))
        events3, _ = fs_poll(cfg, None, cursor2)
        assert len(events3) == 1, f"改动没被识别: {events3}"
        assert events3[0].key != events[0].key
        print("sources: ok")


if __name__ == "__main__":
    demo()
