"""端到端自检:真跑 tick / 事件幂等 / 审批挂起 / 续跑,只有模型是假的。

这是那「一个能跑的检查」。逻辑一坏它就红,不需要 API key,几秒钟出结果:
    python -m pytest tests/ -q
    python -m dots selftest
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langgraph.types import Command

from dots import db, graph, skills as skills_mod, worker


# ------------------------------------------------------------------ 假模型

class _State:
    def __init__(self, messages=None, next_=()):
        self.values = {"messages": list(messages or [])}
        self.next = tuple(next_)


class _Interrupt:
    def __init__(self, value):
        self.value = value


def _ai(text: str, tokens: int = 0) -> AIMessage:
    msg = AIMessage(content=text)
    if tokens:
        msg.usage_metadata = {
            "input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens,
        }
    return msg


class StubAgent:
    """按脚本回应。script 每一项是一次 invoke 的返回值。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, object]] = []
        self._states: dict[str, _State] = {}

    def get_state(self, config):
        return self._states.get(config["configurable"]["thread_id"], _State())

    def invoke(self, payload, config):
        tid = config["configurable"]["thread_id"]
        self.calls.append((tid, payload))
        result = self.script.pop(0) if self.script else {"messages": [_ai("ok")]}
        self._states[tid] = _State(result.get("messages"))
        return result


# ------------------------------------------------------------------- 夹具

def _cfg(root: Path):
    cfg = SimpleNamespace(
        root=root,
        workspace=root / "workspace",
        db=root / "dot.db",
        tick_seconds=1,
        model=SimpleNamespace(
            name="fake:model", base_url=None, max_tokens=64, temperature=0.0
        ),
        smtp=SimpleNamespace(usable=False, host="", port=587, user="", sender="",
                             to="", password_env="X", use_tls=True, password=""),
        sources=SimpleNamespace(
            fs=SimpleNamespace(enabled=True, paths=["inbox"], interval=60),
            imap=SimpleNamespace(enabled=False, host="", port=993, user="", folder="INBOX",
                                 password_env="X", interval=60, password=""),
            ical=SimpleNamespace(enabled=False, url="", interval=60),
        ),
        # save_skill 要显式放行,否则 _gate 会去调 interrupt(),而这里没有图
        rules={"send_email": "ask", "run_shell": "deny", "save_skill": "allow"},
        approval_subject_prefix="[dot#",
        approval_timeout_hours=72,
        daily_token_budget=0,
        bots={
            "dot": SimpleNamespace(name="dot", prompt="", tools=[]),
            "writer": SimpleNamespace(name="writer", prompt="写作", tools=[]),
            "ops": SimpleNamespace(name="ops", prompt="执行", tools=["send_email"]),
        },
        skills=SimpleNamespace(enabled=True, dir="skills"),
        handoff=SimpleNamespace(max_hops=2),
    )
    # 真的 Config 上这是个方法,桩也得有
    cfg.bot = lambda name: cfg.bots.get(name) or SimpleNamespace(
        name=name, prompt="", tools=[]
    )
    return cfg


def _setup(root: Path):
    cfg = _cfg(root)
    (cfg.workspace / "inbox").mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db)
    db.init(conn)
    graph.CURRENT = graph.RunCtx(cfg=cfg, conn=conn)
    return cfg, conn


# --------------------------------------------------------------------- 主流程

def run() -> str:
    """返回一句人话的结论。pytest 之外也用它。"""
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg, conn = _setup(root)
        try:
            notes.append(_case_event_to_goal(cfg, conn))
            notes.append(_case_idempotent(cfg, conn))
            notes.append(_case_interrupt_and_resume(cfg, conn))
            notes.append(_case_approval_reply_is_not_a_goal(cfg, conn))
            notes.append(_case_self_write_is_not_a_loop(cfg, conn))
            notes.append(_case_approval_expiry(cfg, conn))
            notes.append(_case_save_skill_validated(cfg, conn))
            notes.append(_case_handoff(cfg, conn))
            notes.append(_case_atomic_claim_and_recovery(cfg, conn))
        finally:
            # Windows 上不关连接就删不掉这个目录
            conn.close()
    return "\n".join(notes)


def _case_event_to_goal(cfg, conn) -> str:
    (cfg.workspace / "inbox" / "a.txt").write_text("hello", encoding="utf-8")
    agent = StubAgent([{"messages": [_ai("处理完了", tokens=100)]}])
    stats = worker.tick(cfg, conn, agent)

    assert stats["events_new"] == 1, stats
    assert stats["goals_new"] == 1, stats
    assert stats["ran"] == 1, stats

    goals = db.list_goals(conn)
    assert len(goals) == 1, [dict(g) for g in goals]
    # 假模型没调 complete_goal,所以必须落在待复核,不能算 done
    assert goals[0]["status"] == db.REVIEW, goals[0]["status"]
    assert goals[0]["tokens"] == 100, goals[0]["tokens"]

    kinds = {r["kind"] for r in db.recent_audit(conn, 50)}
    assert {"goal_from_event", "run_start", "run_done", "no_completion"} <= kinds, kinds
    return "  文件事件 -> 建目标 -> 跑一轮 -> 没给证据就进待复核  ✓"


def _case_idempotent(cfg, conn) -> str:
    before = len(db.list_goals(conn))
    agent = StubAgent()
    stats = worker.tick(cfg, conn, agent)
    assert stats["events_new"] == 0, stats
    assert stats["goals_new"] == 0, stats
    assert agent.calls == [], "没有新事件却调了模型"
    assert len(db.list_goals(conn)) == before
    return "  同一个文件再扫一遍 -> 不重复触发  ✓"


def _case_interrupt_and_resume(cfg, conn) -> str:
    # 造一个 ready 的目标,让第一轮 invoke 直接中断
    gid = db.add_goal(conn, "帮我给 alice 发一封周报", origin="cli", status=db.READY)
    agent = StubAgent([{"__interrupt__": (_Interrupt(
        {"action": "send_email", "args": {"to": "alice@example.com", "subject": "周报"},
         "goal_id": gid}),)}])
    stats = worker.tick(cfg, conn, agent)

    assert stats["interrupted"] == 1, stats
    assert db.get_goal(conn, gid)["status"] == db.INTERRUPTED
    pending = db.pending_approvals(conn, gid)
    assert len(pending) == 1, pending
    token = pending[0]["token"]

    # 没批之前,下一轮不能动它
    agent2 = StubAgent()
    worker.tick(cfg, conn, agent2)
    assert agent2.calls == [], "还没批就把目标跑了"
    assert db.get_goal(conn, gid)["status"] == db.INTERRUPTED

    # 模拟人在 CLI 上批了
    assert db.resolve_approval(conn, token, "approved") is True
    assert db.resolve_approval(conn, token, "approved") is False, "重复批准没被挡住"

    agent3 = StubAgent([{"messages": [_ai("邮件已发", tokens=50)]}])
    stats = worker.tick(cfg, conn, agent3)
    assert stats["resumed"] == 1, stats
    assert len(agent3.calls) == 1
    payload = agent3.calls[0][1]
    assert isinstance(payload, Command), payload
    assert payload.resume == {"approved": True}, payload.resume
    return "  要求审批 -> 挂起不动 -> 批准后才续跑,续跑值正确  ✓"


def _case_approval_reply_is_not_a_goal(cfg, conn) -> str:
    goals_before = len(db.list_goals(conn))

    # 一封「看不懂正文」的审批回信:不能变成目标,也不能被当成批准
    db.add_event(conn, "imap", "uid:900", {
        "subject": "[dot#beef] 发邮件给 alice", "body": "这是一段没有人话的回复",
        "from": "me@example.com",
    })
    worker.tick(cfg, conn, StubAgent())
    assert len(db.list_goals(conn)) == goals_before, "审批回信被当成了新目标"
    kinds = {r["kind"] for r in db.recent_audit(conn, 20)}
    assert "approval_unparsed" in kinds, kinds

    # 一封真正的批准回信
    gid = db.add_goal(conn, "x", origin="cli", status=db.READY)
    db.create_approval(conn, "cafe", gid, "send_email", {"to": "b@example.com"})
    db.set_goal(conn, gid, status=db.INTERRUPTED)
    db.add_event(conn, "imap", "uid:901", {
        "subject": "[dot#cafe] 发邮件给 bob", "body": "同意\n\n> dot 想执行一个动作...",
        "from": "me@example.com",
    })
    stats = worker.tick(cfg, conn, StubAgent([{"messages": [_ai("ok")]}]))
    assert stats["approval_replies"] == 1, stats
    assert db.get_approval(conn, "cafe")["status"] == "approved"
    assert len(db.list_goals(conn)) == goals_before + 1, "批准回信又建了一个目标"
    return "  审批回信不会被当成新目标,正文看不懂就不批  ✓"


def _case_self_write_is_not_a_loop(cfg, conn) -> str:
    """agent 往被监听的目录里写东西,不能把自己喂起来。

    这是主动 agent 最典型的死法:处理 todo.txt -> 写出 todo_review.md ->
    被自己的 fs 源当成新文件 -> 再处理一遍 -> 又写一遍。
    """
    goals_before = len(db.list_goals(conn))
    db.add_goal(conn, "把结论写进 inbox/", origin="cli", status=db.READY)

    class Writer(StubAgent):
        def invoke(self, payload, config):
            (cfg.workspace / "inbox" / "out.md").write_text("报告", encoding="utf-8")
            return super().invoke(payload, config)

    worker.tick(cfg, conn, Writer([{"messages": [_ai("写好了")]}]))

    stats = worker.tick(cfg, conn, StubAgent())
    assert stats["events_new"] == 0, f"agent 自己写的文件触发了新一轮: {stats}"
    assert stats["goals_new"] == 0, stats
    assert len(db.list_goals(conn)) == goals_before + 1, "自产文件建出了新目标"

    kinds = {r["kind"] for r in db.recent_audit(conn, 20)}
    assert "suppressed_self_writes" in kinds, kinds

    # 失败的 run 同样会在工作区留下半成品。这条是踩了坑才加的:
    # 原来 _suppress_self_writes 在 return 之后,失败路径根本不执行,
    # 结果一个模型报错的目标能靠自己的半成品再长出好几个目标。
    class FailingWriter(StubAgent):
        def invoke(self, payload, config):
            (cfg.workspace / "inbox" / "half_done.md").write_text("半成品", encoding="utf-8")
            raise RuntimeError("模型挂了")

    db.add_goal(conn, "写一半就崩", origin="cli", status=db.READY)
    stats = worker.tick(cfg, conn, FailingWriter())
    assert stats["failed"] == 1, stats

    stats = worker.tick(cfg, conn, StubAgent())
    assert stats["events_new"] == 0, f"失败 run 的半成品触发了新一轮: {stats}"
    assert stats["goals_new"] == 0, stats
    return "  agent 自产的文件(含失败 run 的半成品)不会把自己喂起来  ✓"


def _case_approval_expiry(cfg, conn) -> str:
    """挂起的动作超时后必须自动按「拒绝」处理,否则目标永远卡在等你。"""
    gid = db.add_goal(conn, "发一封没人批的邮件", origin="cli", status=db.READY)
    db.create_approval(conn, "dead", gid, "send_email", {"to": "x@example.com"})
    db.set_goal(conn, gid, status=db.INTERRUPTED)
    # 伪装成 100 小时前挂起的(默认超时 72h)
    conn.execute(
        "UPDATE approvals SET created_at = ? WHERE token = 'dead'",
        (db.now() - 100 * 3600,),
    )
    conn.commit()

    agent = StubAgent([{"messages": [_ai("好吧,那我不发了")]}])
    stats = worker.tick(cfg, conn, agent)

    assert db.get_approval(conn, "dead")["status"] == "expired"
    assert stats["resumed"] == 1, stats
    assert agent.calls, "超时的目标没有被放行,它卡住了"
    # 超时往安全的方向倒:算拒绝,不是算批准
    assert agent.calls[0][1].resume == {"approved": False}, agent.calls[0][1].resume
    kinds = {r["kind"] for r in db.recent_audit(conn, 30)}
    assert "approvals_expired" in kinds, kinds
    return "  审批超时自动按拒绝处理,目标被放行而不是永远卡住  ✓"


def _case_save_skill_validated(cfg, conn) -> str:
    """不合规的描述必须被拒 —— 描述每轮都进系统提示并被截断,超了等于没写。"""
    graph.CURRENT.goal_id = "x"
    graph.CURRENT.bot = "dot"
    tools = {t.name: t for t in graph.make_tools(graph.CURRENT, cfg.bot("dot"))}
    save = tools["save_skill"]

    bad_name = save.invoke({"name": "Summarize Inbox!", "description": "Does it.", "body": "b"})
    assert "不合规" in bad_name, bad_name

    desc_61 = "y" * 60 + "."
    too_long = save.invoke({"name": "ok-name", "description": desc_61, "body": "b"})
    assert "上限 60" in too_long, too_long
    # 拒绝理由里要报出实际长度,不然模型不知道该砍多少
    assert str(len(desc_61)) in too_long, too_long

    marketing = save.invoke(
        {"name": "ok-name", "description": "A comprehensive powerful thing.", "body": "b"}
    )
    assert "营销词" in marketing, marketing
    assert not skills_mod.scan(cfg), "被拒的技能不该落盘"

    good = save.invoke({
        "name": "summarize-inbox",
        "description": "Turn a dropped file into a prioritized action list.",
        "body": "## Steps\n1. Read it.\n2. Sort it.",
    })
    assert "已存到" in good, good
    found = {s.name: s for s in skills_mod.scan(cfg)}
    assert "summarize-inbox" in found, found
    assert found["summarize-inbox"].description == (
        "Turn a dropped file into a prioritized action list."
    )
    assert "## Steps" in found["summarize-inbox"].body

    # 技能是自修改:关掉开关就得真的存不进去
    cfg.skills.enabled = False
    off = save.invoke({"name": "another", "description": "Does a thing.", "body": "b"})
    assert "关掉" in off, off
    cfg.skills.enabled = True
    return "  save_skill:不合规的描述被拒,合规的真落盘,开关生效  ✓"


def _case_handoff(cfg, conn) -> str:
    """交接:换 bot、记简报、有次数上限。"""
    tools = {t.name: t for t in graph.make_tools(graph.CURRENT, cfg.bot("dot"))}
    handoff = tools["handoff_to"]

    # 先测跑起来之后的行为:交接完就算 agent 顺手 complete_goal 也不算数
    gid = db.add_goal(conn, "先交接再接活", origin="cli", status=db.READY)
    seen: dict = {}

    class HandoffThenComplete(StubAgent):
        def invoke(self, payload, config):
            graph.CURRENT.goal_id = gid
            seen["msg"] = handoff.invoke(
                {"bot": "writer", "reason": "这是写作任务", "note": "材料在 out.md"}
            )
            # 交接之后还硬要把目标标成完成 —— worker 必须盖回去
            db.set_goal(conn, gid, status=db.DONE, summary="我自己做完了")
            return {"messages": [_ai("done")]}

    stats = worker.tick(cfg, conn, HandoffThenComplete())
    assert stats["handoffs"] == 1, stats
    g = db.get_goal(conn, gid)
    assert g["bot"] == "writer", g["bot"]
    assert g["status"] == db.READY, f"交接后的完成没被盖回去: {g['status']}"
    assert g["handoffs"] == 1
    assert "写作任务" in g["handoff_note"]
    # 简报要传给接手方
    kickoff = worker._kickoff_text(g)
    assert "dot 把这件事交给了你" in kickoff and "out.md" in kickoff, kickoff

    # 工具层的边界
    db.set_goal(conn, gid, status=db.REVIEW)   # 挪开,别干扰后面的 pick_ready
    gid2 = db.add_goal(conn, "边界", origin="cli", status=db.READY)
    graph.CURRENT.goal_id = gid2
    graph.CURRENT.bot = "dot"

    assert "不能交给自己" in handoff.invoke({"bot": "dot", "reason": "r", "note": "n"})
    assert "没有叫" in handoff.invoke({"bot": "nope", "reason": "r", "note": "n"})

    conn.execute(
        "UPDATE goals SET handoffs = ? WHERE id = ?", (cfg.handoff.max_hops, gid2)
    )
    conn.commit()
    blocked = handoff.invoke({"bot": "writer", "reason": "r", "note": "n"})
    assert "到上限" in blocked, blocked
    assert db.get_goal(conn, gid2)["bot"] == "dot", "到上限了还是交出去了"
    return "  交接:换人+留简报,交接后的完成被盖回,自交/未知 bot/乒乓都被拦  ✓"


def _case_atomic_claim_and_recovery(cfg, conn) -> str:
    """目标只能被抢一次;进程被 kill 留下的 running 要能收回来。"""
    gid = db.add_goal(conn, "只能被抢一次", origin="cli", status=db.READY)

    assert db.claim_goal(conn, gid, db.READY) is True
    assert db.claim_goal(conn, gid, db.READY) is False, "同一个目标被抢了两次"
    assert db.claim_goal(conn, gid, db.INTERRUPTED) is False, "状态不对也能抢"
    assert db.get_goal(conn, gid)["status"] == db.RUNNING

    # 伪装成 1 小时前被 kill 掉、卡在 running 的目标
    conn.execute(
        "UPDATE goals SET updated_at = ? WHERE id = ?", (db.now() - 3600, gid)
    )
    conn.commit()
    assert worker.recover_stale_runs(cfg, conn) == 1
    assert db.get_goal(conn, gid)["status"] == db.READY
    return "  目标只能原子抢一次;僵死的 running 会被回收重跑  ✓"


# ------------------------------------------------------------------- pytest

def test_loop():
    run()


def test_rules():
    from dots import rules

    rules.demo()


def test_approvals_parse():
    from dots import approvals

    approvals.demo()


if __name__ == "__main__":
    print(run())
