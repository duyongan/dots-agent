"""活动视图的测试。

重点不是「页面能不能出来」,是**转义**:
目标文本来自模型,邮件主题来自外部,审批参数来自模型的工具调用 ——
任何一处没转义,就是往你自己的 dashboard 里投 XSS。

    python -m pytest tests/test_web.py -q
    python -m tests.test_web
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.error
import urllib.request

from dot import db, web

_TEMP_DBS: list[tuple[object, str]] = []


def _tmp_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    db.init(conn)
    _TEMP_DBS.append((conn, path))
    return conn, path


def _cleanup():
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


def _get(port: int, path: str = "/") -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.read().decode("utf-8")


def test_empty_dashboard() -> str:
    conn, path = _tmp_conn()
    srv = web.start(path, port=0)
    try:
        port = srv.server_address[1]
        page = _get(port)
        assert "还没有目标" in page and "没有等着你的动作" in page, page[:400]
        assert "还没有活动" in page
        # 未知路径必须 404,别把整个文件系统当路由
        try:
            _get(port, "/../../etc/passwd")
            raise AssertionError("未知路径没有 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code
    finally:
        srv.shutdown()
        srv.server_close()
    return "  空库页面正常,未知路径 404  ✓"


def test_dashboard_shows_data() -> str:
    conn, path = _tmp_conn()
    gid = db.add_goal(conn, "把 inbox 整理成报告", origin="cli", status=db.READY)
    db.set_goal(conn, gid, status=db.DONE, summary="写好了", evidence="inbox/report.md")
    db.add_tokens(conn, gid, 45926)
    db.create_approval(conn, "ab12", gid, "send_email", {"to": "alice@example.com"})
    db.audit(conn, gid, "complete_goal", "写好了")

    srv = web.start(path, port=0)
    try:
        page = _get(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()

    for needle in ("把 inbox 整理成报告", "inbox/report.md", "45,926", "ab12",
                   "send_email", "alice@example.com", "complete_goal"):
        assert needle in page, f"页面里少东西: {needle}"
    return "  目标/待批/活动三张表都渲染出数据了  ✓"


def test_model_text_is_escaped() -> str:
    """模型和邮件里的文本一律当不可信内容处理。"""
    conn, path = _tmp_conn()
    payload = "<script>alert('xss')</script>"
    gid = db.add_goal(conn, payload, origin="cli", status=db.READY)
    db.set_goal(conn, gid, status=db.DONE, summary=payload, evidence=payload)
    db.create_approval(conn, "beef", gid, "send_email", {"to": payload})
    db.audit(conn, gid, payload, payload)

    srv = web.start(path, port=0)
    try:
        page = _get(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()

    # 原始标签一个都不许出现在渲染结果里
    assert "<script" not in page.lower(), "script 标签漏进页面了"
    assert "alert('xss')" not in page or "&lt;script&gt;" in page, "转义没生效"
    # 转义后的形态应该出现,证明内容确实渲染了而不是被吞掉
    assert "&lt;script&gt;" in page, "内容被吞掉了,而不是被转义"
    return "  模型/邮件来的文本全部转义,注入不进去  ✓"


def run() -> str:
    import contextlib
    import io

    cases = [test_empty_dashboard, test_dashboard_shows_data, test_model_text_is_escaped]
    notes = []
    try:
        for case in cases:
            # web.start 会打一行「活动视图 http://...」,自检输出里不需要
            with contextlib.redirect_stdout(io.StringIO()):
                notes.append(case())
    finally:
        _cleanup()
    return "\n".join(notes)


def test_all_web():
    run()


if __name__ == "__main__":
    print(run())
