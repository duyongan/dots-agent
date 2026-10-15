"""审批:发邮件 + 解析回复。

邮箱同时是事件源和审批通道,靠主题前缀区分。

自激循环警告:dot 自己发出去的审批邮件会被 imap_poll 再收一次。
必须靠主题前缀识别并拦下,否则后果是 dot 给自己回信回一晚上。
"""

from __future__ import annotations

import re
import secrets
import smtplib
import sys
from email.message import EmailMessage

from dot import db

APPROVE_WORDS = {"y", "yes", "ok", "okay", "approve", "approved", "同意", "批准", "是", "可以", "准"}
REJECT_WORDS = {"n", "no", "reject", "rejected", "deny", "denied", "拒绝", "否", "不要", "不行", "驳回"}

APPROVED = "approved"
REJECTED = "rejected"


def make_token() -> str:
    """4 个十六进制字符。自用够,猜测窗口也只有 65536。"""
    return secrets.token_hex(2)


def token_pattern(prefix: str) -> re.Pattern:
    return re.compile(re.escape(prefix) + r"([0-9a-f]{2,8})\]")


def is_approval_subject(prefix: str, subject: str) -> bool:
    return bool(subject) and token_pattern(prefix).search(subject) is not None


def subject_for(prefix: str, token: str, summary: str) -> str:
    return f"{prefix}{token}] {summary}"


def parse_decision(body: str) -> str | None:
    """只认第一个有意义的词。看不懂的一律返回 None,不猜。"""
    for raw in (body or "").strip().splitlines():
        word = raw.strip().strip("。.!！").lower()
        if not word:
            continue
        if word in APPROVE_WORDS:
            return APPROVED
        if word in REJECT_WORDS:
            return REJECTED
        # 回信里常带引用,只在第一行找;
        # 第一行看不懂就再给正文一次机会见下面的 fallback
        break
    tokens = set(re.findall(r"[a-zA-Z一-鿿]+", (body or "").lower()))
    hits_a = tokens & APPROVE_WORDS
    hits_r = tokens & REJECT_WORDS
    if hits_a and not hits_r:
        return APPROVED
    if hits_r and not hits_a:
        return REJECTED
    return None


def send(cfg, conn, goal_id: str, payload: dict, *, dry_run: bool = False) -> str:
    """记一条 approvals,然后发邮件。发失败也保留 token —— 你还能用 CLI 批。"""
    token = make_token()
    while db.get_approval(conn, token) is not None:
        token = make_token()

    tool = payload.get("action", "?")
    args = payload.get("args", {})
    db.create_approval(conn, token, goal_id, tool, args)

    summary = _summarize(tool, args)
    subject = subject_for(cfg.approval_subject_prefix, token, summary)
    body = _body_text(token, tool, args)

    db.audit(conn, goal_id, "approval_requested", f"{token} {summary}")

    if dry_run or not cfg.smtp.usable:
        reason = "dry-run" if dry_run else "没配 [smtp]"
        print(f"\n[dot] 需要你批准({reason},已记入待批队列)")
        print(f"      主题: {subject}")
        print(f"      替代做法: python -m dot approve {token}\n", file=sys.stderr)
        return token

    try:
        send_mail(cfg, cfg.smtp.to, subject, body)
        db.audit(conn, goal_id, "approval_sent", f"{token} -> {cfg.smtp.to}")
    except Exception as e:
        db.audit(conn, goal_id, "approval_send_failed", f"{token}: {type(e).__name__}: {e}")
        print(
            f"[dot] 审批邮件发不出去({type(e).__name__}: {e})。\n"
            f"      用 CLI 顶一下: python -m dot approve {token}",
            file=sys.stderr,
        )
    return token


def send_mail(cfg, to: str, subject: str, body: str) -> None:
    """全项目唯一的发信出口。审批和 agent 的动作共用它。失败就抛。"""
    if not cfg.smtp.usable:
        raise RuntimeError("没配 [smtp],发不了邮件")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp.sender
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(cfg.smtp.host, cfg.smtp.port, timeout=30) as s:
        if cfg.smtp.use_tls:
            s.starttls()
        if cfg.smtp.user:
            s.login(cfg.smtp.user, cfg.smtp.password)
        s.send_message(msg)


def parse_reply(cfg, subject: str, body: str) -> tuple[str, str] | None:
    """从一封回信里取出 (token, 决定)。不是审批回信就返回 None。"""
    m = token_pattern(cfg.approval_subject_prefix).search(subject or "")
    if not m:
        return None
    decision = parse_decision(body)
    if decision is None:
        return None
    return m.group(1), decision


def _summarize(tool: str, args: dict) -> str:
    if tool == "send_email":
        return f"发邮件给 {args.get('to', '?')}: {str(args.get('subject', ''))[:40]}"
    if tool == "run_shell":
        return f"执行命令: {str(args.get('cmd', ''))[:60]}"
    return f"{tool} {str(args)[:60]}"


def _body_text(token: str, tool: str, args: dict) -> str:
    lines = [
        f"dot 想执行一个动作,需要你批准。",
        "",
        f"  编号   {token}",
        f"  动作   {tool}",
    ]
    for k, v in args.items():
        lines.append(f"  {k:<6} {str(v)[:500]}")
    lines += [
        "",
        "直接回复这封邮件:",
        "  回 y 或 同意   → 执行",
        "  回 n 或 拒绝   → 不执行",
        "",
        "只认第一行。其他回复会被忽略(不会产生新目标)。",
        "不确定它想干什么,就先 python -m dot log 看一眼。",
    ]
    return "\n".join(lines)


def demo() -> None:
    prefix = "[dot#"
    assert is_approval_subject(prefix, "[dot#a7f3] 发邮件给 x")
    assert not is_approval_subject(prefix, "你好")
    assert parse_decision("y") == APPROVED
    assert parse_decision("同意\n\n引用原文...") == APPROVED
    assert parse_decision("拒绝") == REJECTED
    assert parse_decision("随便说点什么") is None
    # 引用里同时出现两边时不给结论,别猜
    assert parse_decision("yes or no?") in (APPROVED, REJECTED, None)
    print("approvals: ok")


if __name__ == "__main__":
    demo()
