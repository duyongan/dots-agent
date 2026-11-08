"""缺陷指纹与台账的自检:同一条错只开一个目标。

指纹是去重的全部依据。这里重点盯两件事:
  1. 同一条错换个说法、换个行号,必须还是同一个指纹;
  2. 修完/关掉之后又出现,必须能被认成「回归」,而不是安静地留在已修复里。
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

from dots import bugs, db


def _conn(tag: str = "bugs"):
    root = Path.cwd() / f".dots-test-{tag}-{secrets.token_hex(3)}"
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "dot.db")
    db.init(conn)
    return conn, root


def test_report_detection() -> str:
    assert bugs.should_report("订单导出报错了,500")
    assert bugs.should_report("[订单] NullPointerException")
    assert not bugs.should_report("好的收到")
    assert not bugs.should_report("今天发版吗")

    # 配置里能覆盖关键词
    assert bugs.should_report("支付挂了", keywords=["挂了"])
    assert not bugs.should_report("支付挂了", keywords=["超时"])
    return "判定:只有像报错的话才进台账"


def test_fingerprint_stability() -> str:
    a = "订单导出报错 java.lang.NullPointerException at OrderService.java:42"
    b = "又是这个 java.lang.NullPointerException\nOrderService.java:88"
    c = "订单导出报错 java.lang.NullPointerException at RefundService.java:12"

    assert bugs.fingerprint(a) == bugs.fingerprint(b), "行号不该让指纹漂移"
    assert bugs.fingerprint(a) != bugs.fingerprint(c), "换了文件就是另一个缺陷"
    assert bugs.signature(a).startswith("exc=NullPointerException")

    # 没有结构信息时退回正文骨架:数字/标点/空白不该影响
    x = "接口 500 了,订单 123 导出失败"
    y = "接口500了 订单456导出失败"
    assert bugs.fingerprint(x) == bugs.fingerprint(y), "错误码相同就是同一个缺陷"
    assert bugs.fingerprint("随便一句话") == bugs.fingerprint("随便一句话。")
    return "指纹:换行号/换措辞仍然是一个缺陷"


def test_severity_and_title() -> str:
    assert bugs.severity("线上支付全挂了") == "P0"
    assert bugs.severity("接口报 500") == "P1"
    assert bugs.severity("按钮样式不对") == "P3"
    assert bugs.severity("有点问题") == "P2"
    assert bugs.title("  \n\n[订单] 导出报错 at 12:30").startswith("[订单] 导出报错")
    assert bugs.module_of("[订单模块] 报错") == "订单模块"
    return "分级:P0/P1/P2/P3 按关键词落,标题取第一行有效文字"


def test_ledger_dedupes() -> str:
    conn, root = _conn()
    try:
        text = "订单导出报错 NullPointerException at OrderService.java:42"
        row1, fresh1, n1 = bugs.record(conn, text, group_name="项目A", reporter="张三")
        assert fresh1 and n1 == 1 and row1["status"] == "open"
        assert row1["severity"] == "P1"

        row2, fresh2, n2 = bugs.record(
            conn, "同一个错 OrderService.java:99 NullPointerException", group_name="项目A")
        assert not fresh2, "行号变了但根因相同,不该新建"
        assert n2 == 2 and row2["id"] == row1["id"]
        assert len(db.list_bugs(conn)) == 1

        # 修完再报 = 回归,要重新开一条
        db.set_bug(conn, row1["id"], status=db.BUG_FIXED)
        row3, fresh3, n3 = bugs.record(conn, text, group_name="项目A")
        assert fresh3, "修完又出现应该算回归"
        assert row3["id"] == row1["id"] and row3["status"] == "open"
        assert n3 == 3

        assert len(db.open_bugs(conn)) == 1
        db.set_bug(conn, row1["id"], status=db.BUG_CLOSED)
        assert db.open_bugs(conn) == []
        return "台账:重复报合并、回归重开、关掉后不再进待修列表"
    finally:
        conn.close()
        shutil.rmtree(root, ignore_errors=True)


def test_requirement_text() -> str:
    conn, root = _conn("bugreq")
    try:
        row, _, _ = bugs.record(conn, "接口 502,订单服务超时", group_name="项目A",
                                reporter="李四")
        text = bugs.requirement_text([row])
        assert row["id"] in text and "订单服务超时" in text
        assert "502" in text and "P1" in text

        goal = bugs.goal_text(row)
        assert "先复现" in goal and row["id"] in goal
        return "派单:缺陷能拼成修复流水线的需求 / 普通目标的文本"
    finally:
        conn.close()
        shutil.rmtree(root, ignore_errors=True)


def run() -> str:
    notes = [
        test_report_detection(),
        test_fingerprint_stability(),
        test_severity_and_title(),
        test_ledger_dedupes(),
        test_requirement_text(),
    ]
    return "缺陷:" + ";".join(notes)


def test_bugs():                           # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
