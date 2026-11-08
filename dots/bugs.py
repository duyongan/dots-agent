"""缺陷:从一段群聊/邮件里认出一个 BUG,给它一个稳定指纹,记进台账。

为什么要指纹:群里十个人报同一条错,或者一个人把同一条错发三遍。没有指纹,
就是十个目标、十次模型调用、十条 Jira 单,最后没人知道修的是不是同一件事。

指纹怎么算(越靠前越优先):
  1. 异常类型 + 顶部文件名        `exc=NullPointerException|file=OrderService.java`
  2. HTTP 状态码 / 错误码          `code=500`
  3. 没有结构信息时,取归一化后的正文前 80 字
只取「顶部文件」不取行号 —— 行号会随代码改动漂移,拿它当指纹等于每次都新报一条。
"""

from __future__ import annotations

import hashlib
import json
import re

from dots import db

# 判定「这句话是在报 BUG」的关键词。配置里可以覆盖。
DEFAULT_KEYWORDS = (
    "bug", "报错", "异常", "崩溃", "闪退", "挂了", "失败", "超时", "无法",
    "不能", "白屏", "卡死", "空指针", "报障", "修复一下", "复现",
    "error", "exception", "traceback", "fatal", "panic", "timeout",
    "500", "502", "503", "504",
)

# 严重度:命中越靠前越严重。顺序有意义。
_SEVERITY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("P0", ("线上", "全网", "全站", "宕机", "崩溃", "数据丢失", "支付",
            "无法下单", "紧急", "p0", "阻塞", "挂了")),
    ("P1", ("500", "502", "503", "504", "异常", "失败", "超时", "报错",
            "error", "exception", "白屏", "panic", "无法")),
    ("P3", ("样式", "文案", "错别字", "界面", "按钮", "提示语", "ui", "排版")),
]

_EXC_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout|Failure|Panic))\b")
_FILE_RE = re.compile(
    r"([\w./\\-]+\.(?:py|java|go|ts|tsx|js|jsx|sql|php|rb|cs|kt))(?::\d+)?")
_CODE_RE = re.compile(r"(?<![\d])(?:[45]\d{2}|E\d{4,}|ERR_[A-Z0-9_]+)(?![\d])")
_MODULE_RE = re.compile(r"[\[【]([^\]】]{2,20})[\]】]")
_SPACES = re.compile(r"\s+")
_NOISE = re.compile(r"[\d\W_]+", re.UNICODE)


def should_report(text: str, keywords=None) -> bool:
    """这句话像不像在报 BUG。

    太短的(「嗯」「好的」)直接放过 —— 除非用户自己在配置里指定了关键词,
    那时候他显然知道「挂了」这种两个字的话也要算。
    """
    body = (text or "").strip()
    if not body:
        return False
    words = tuple(keywords) if keywords else DEFAULT_KEYWORDS
    if not keywords and len(body) < 4:
        return False
    low = body.lower()
    return any(str(w).lower() in low for w in words)


def signature(text: str) -> str:
    """结构化指纹的来源。空字符串 = 没有结构信息,走正文兜底。"""
    body = text or ""
    parts = []
    exc = _EXC_RE.findall(body)
    if exc:
        parts.append("exc=" + exc[0])
    files = _FILE_RE.findall(body)
    if files:
        top = files[0].replace("\\", "/").split("/")[-1]
        parts.append("file=" + top)
    codes = _CODE_RE.findall(body)
    if codes:
        parts.append("code=" + codes[0])
    return "|".join(parts)


def _normalized(text: str, limit: int = 80) -> str:
    """去掉数字、标点、空白,只留语义骨架。空格和错别字的差异就被抹平了。"""
    return _NOISE.sub("", str(text or "").lower())[:limit]


def fingerprint(text: str) -> str:
    sig = signature(text)
    basis = sig if sig else "text=" + _normalized(text)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def severity(text: str) -> str:
    """P0 / P1 / P2 / P3。规则很粗,但比「全按 P2」有用得多。"""
    low = (text or "").lower()
    for level, words in _SEVERITY_RULES:
        if any(w in low for w in words):
            return level
    return "P2"


def title(text: str, *, limit: int = 60) -> str:
    """取第一行有信息量的文字当标题。"""
    for line in (text or "").splitlines():
        line = _SPACES.sub(" ", line).strip(" -—*#>")
        if len(line) >= 4:
            return line[:limit]
    return (text or "").strip()[:limit] or "(无内容)"


def module_of(text: str) -> str:
    """群里常见写法:[订单模块] 报错了。认不出来的留空,别硬猜。"""
    m = _MODULE_RE.search(text or "")
    if m:
        return m.group(1).strip()
    files = _FILE_RE.findall(text or "")
    if files:
        return files[0].replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
    return ""


def extract(text: str, *, keywords=None) -> dict:
    """一段文本 -> 一条缺陷事实。纯函数,测试直接调它。"""
    body = (text or "").strip()
    return {
        "fingerprint": fingerprint(body),
        "title": title(body),
        "severity": severity(body),
        "module": module_of(body),
        "signature": signature(body),
        "detail": body[:4000],
        "is_bug": should_report(body, keywords),
    }


def record(conn, text: str, *, channel: str = "wechat", group_name: str = "",
           reporter: str = "", keywords=None) -> tuple[dict, bool, int]:
    """落台账。返回 (行, 是否新建, 累计次数)。重复报只加计数。"""
    fact = extract(text, keywords=keywords)
    before = db.find_bug_by_fingerprint(conn, fact["fingerprint"])
    bid, is_new, count = db.upsert_bug(
        conn,
        fingerprint=fact["fingerprint"],
        title=fact["title"],
        detail=fact["detail"],
        severity=fact["severity"],
        channel=channel,
        group_name=group_name,
        reporter=reporter,
        sample=f"{group_name}/{reporter}: {fact['detail'][:200]}",
    )
    row = db.get_bug(conn, bid)
    # 已经修完/关掉的又出现 = 回归,调用方需要重新开一条修复流水线
    reopened = bool(before and before["status"] in db.BUG_TERMINAL)
    return dict(row), is_new or reopened, count


def requirement_text(bug_rows) -> str:
    """把一批缺陷拼成修复流水线的需求原文。"""
    lines = []
    for row in bug_rows:
        lines += [
            f"### {row['id']} [{row['severity']}] {row['title']}",
            f"- 来源:{row['channel']}"
            + (f" / {row['group_name']}" if row["group_name"] else "")
            + (f" / {row['reporter']}" if row["reporter"] else ""),
            f"- 被报 {row['count']} 次,最后 {_clock(row['last_seen'])}",
            "",
            "```",
            str(row["detail"])[:1500],
            "```",
            "",
        ]
    return "\n".join(lines).strip()


def goal_text(bug_row) -> str:
    """不走流水线时,给普通目标用的文本。"""
    return (
        f"【BUG {bug_row['id']} {bug_row['severity']}】{bug_row['title']}\n\n"
        f"来源:{bug_row['channel']}"
        + (f" / {bug_row['group_name']}" if bug_row["group_name"] else "")
        + f",已被报 {bug_row['count']} 次。\n\n"
        f"原始描述:\n{str(bug_row['detail'])[:2000]}\n\n"
        "先复现,再定位根因,修完补一个回归测试。"
        "用 complete_goal 交差,evidence 写改动文件路径和验证命令。"
        "修不动就如实说卡在哪,别猜着改。"
    )


def _clock(ts) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


def demo() -> None:
    """纯逻辑自检:指纹稳定、重复报合并、严重度分级。"""
    a = "订单导出报错 java.lang.NullPointerException at OrderService.java:42"
    b = "又是这个错 java.lang.NullPointerException\nOrderService.java:88 空指针"
    assert should_report(a)
    assert not should_report("好的")
    assert not should_report("今天天气不错")

    assert signature(a).startswith("exc=NullPointerException"), signature(a)
    # 行号不同、文字不同,但根因相同 -> 同一个指纹
    assert fingerprint(a) == fingerprint(b), (signature(a), signature(b))
    assert fingerprint("接口 500 了") != fingerprint("导出超时了")

    assert severity("线上支付全挂了") == "P0"
    assert severity("接口报 500") == "P1"
    assert severity("按钮样式不对") == "P3"
    assert severity("有点问题") == "P2"

    assert module_of("[订单模块] 导出报错") == "订单模块"
    assert module_of(a) == "OrderService"

    fact = extract(a, keywords=["报错"])
    assert fact["is_bug"] and fact["fingerprint"]
    assert title("  \n\n订单导出报错 at 12:30") .startswith("订单导出报错")
    print("bugs: ok")


if __name__ == "__main__":
    demo()
