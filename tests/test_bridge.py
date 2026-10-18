"""bridge 的测试。

分三层:
  纯函数级 —— handle() 的分发逻辑,不用起进程
  线程级   —— tick 循环 / 聊天的 ask 往返 / 退出复位
  进程级   —— 真起一个子进程喂 JSON 行(最后一节)

    python -m pytest tests/test_bridge.py -q
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dots import bridge, db, graph


def test_current_is_thread_local() -> str:
    """tick 和聊天跑在两条线程上,各自的 RunCtx 不能互相踩。

    这是 bridge 能成立的前提:模块全局的话,聊天线程设 chat=True,
    tick 线程就会以为自己在聊天,然后不调 complete_goal。
    """
    graph.set_current(graph.RunCtx(cfg=None, conn=None, bot="main"))
    seen: dict = {}
    # 两个 Event 把顺序钉死:两边都得在对方写完之后再读回来。
    # 光 sleep 不制造并发 —— 主线程那段时间停在 join() 上,没人写,踩不出来;
    # 子线程单独读回自己刚设的值,退化回模块全局也一样会通过。
    wrote = threading.Event()      # 子线程写完了
    done = threading.Event()       # 主线程写完了

    def other() -> None:
        seen["before"] = graph.current()          # 子线程不该看见主线程的
        graph.set_current(graph.RunCtx(cfg=None, conn=None, bot="other"))
        wrote.set()
        seen["saw_main_done"] = done.wait(5)      # 等主线程也写完,再读回来
        seen["after"] = graph.current().bot

    t = threading.Thread(target=other)
    t.start()
    try:
        seen["saw_child_done"] = wrote.wait(5)
        graph.set_current(graph.RunCtx(cfg=None, conn=None, bot="main2"))
        seen["mine"] = graph.current().bot
        done.set()
        t.join(5)

        assert seen.get("saw_child_done") and seen.get("saw_main_done"), f"没握上手:{seen}"
        assert seen["before"] is None, "子线程看到了主线程的 RunCtx"
        assert seen["after"] == "other", f"对方写完再读,子线程读到了别人的:{seen}"
        assert seen["mine"] == "main2", seen
        assert graph.current().bot == "main2", "子线程把主线程的 RunCtx 踩了"
    finally:
        done.set()               # 断言挂了也别让子线程卡在 wait 上
        graph.set_current(None)
    return "  RunCtx 是线程局部的,两条线程同时写也互不干扰  ✓"


def test_use_or_create_is_per_thread() -> str:
    """worker 里那 4 处 `if CURRENT is None` 全换成了 use_or_create,得确定它
    本线程复用、跨线程各建各的、换连接时重新绑上。
    """
    cfg, conn = object(), object()
    main_ctx = graph.use_or_create(cfg, conn)
    seen: dict = {}

    def other() -> None:
        seen["ctx"] = graph.use_or_create(cfg, conn)
        seen["again"] = graph.use_or_create(cfg, conn)

    t = threading.Thread(target=other)
    t.start()
    t.join()

    try:
        assert main_ctx is graph.use_or_create(cfg, conn), "同一条线程里没复用"
        assert seen["ctx"] is not main_ctx, "子线程拿到了主线程的 RunCtx"
        assert seen["again"] is seen["ctx"], "同一条线程里没复用"
        assert seen["ctx"].conn is conn, "ctx 里的连接对不上"

        # 换一条连接必须重新绑上。命令线程是长驻的,cli.main 每条命令自己开一条
        # 连接、跑完就关;不重绑的话第二次「跑一轮」会拿着已关闭的连接去写库。
        conn2 = object()
        again = graph.use_or_create(cfg, conn2)
        assert again is main_ctx, "换连接时不该换 ctx 对象(工具的闭包捏着它)"
        assert again.conn is conn2, "换连接没重新绑上"
        assert again.cfg is cfg
    finally:
        # 断言一挂也得清 —— 否则本线程的 ctx 会留给后面同一会话里的用例。
        graph.set_current(None)
    return "  use_or_create:同线程复用、跨线程各建各的、换连接重绑  ✓"


def test_light_commands_do_not_import_langchain() -> str:
    """`dots ls` / `log` / `cost` 不该付 langchain 的导入成本。

    开发机上这个链路是 8.7 秒(langchain 的 language_models/base.py 会
    `try: from transformers import GPT2TokenizerFast`,把 torch 一起拖进来)。
    列几行目标等 8 秒是不能接受的。

    用子进程跑,因为当前进程里 pytest 早就把 langchain 导进来了。
    """
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        "from dots import cli\n"
        "try:\n"
        "    cli.main(['ls'])          # 没配置会 SystemExit,但 dispatch 之前的导入已经全走完了\n"
        "except BaseException:\n"
        "    pass\n"
        "heavy = sorted({m.split('.')[0] for m in sys.modules}\n"
        "               & {'deepagents', 'langchain', 'langchain_core', 'langgraph',\n"
        "                  'torch', 'transformers'})\n"
        "assert not heavy, heavy\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(root),
        capture_output=True, text=True, timeout=120,
        # 不带 encoding 就用 locale(本机 GBK)解码子进程的 UTF-8 输出 ——
        # 会崩在 UnicodeDecodeError 上,而真正的原因(拖了 langchain)反而看不见。
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"import dots.cli 时拖进来了:{proc.stderr[-800:]}"
    return "  dots.cli 不再连带导入 langchain / torch  ✓"


def test_out_writes_one_json_line() -> str:
    """一行一个 JSON,且行内不能有换行 —— 对面是按行读的。"""
    buf = io.StringIO()
    out = bridge.Out(buf)
    out.event("log", level="info", text="新事件 1\n跑完 1")

    line = buf.getvalue()
    assert line.endswith("\n"), repr(line)
    assert line.count("\n") == 1, f"一个事件吐出了多行:{line!r}"
    payload = json.loads(line)
    assert payload["event"] == "log"
    assert payload["text"] == "新事件 1\n跑完 1", "换行被吃掉了"
    return "  Out 一行一个 JSON,内容里的换行被转义  ✓"


def test_out_reply_shape() -> str:
    buf = io.StringIO()
    out = bridge.Out(buf)
    out.reply(7, {"gid": "43e86d"})
    out.reply(8, error="没有叫 nope 的 bot")
    out.reply(9)

    a, b, c = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert a == {"id": 7, "ok": True, "data": {"gid": "43e86d"}}, a
    assert b == {"id": 8, "ok": False, "error": "没有叫 nope 的 bot"}, b
    assert c == {"id": 9, "ok": True, "data": {}}, c
    return "  应答格式:成功带 data,失败带 error  ✓"


class _Yielding(io.StringIO):
    """write 里让一次 GIL,而且把一次 write 劈成两半。

    为什么非劈不可:Out.raw 是「一次 write 写完整行」,而 io.StringIO.write
    在 GIL 下是原子的。只在 super().write() **之前** sleep 的话,只是把
    「轮到谁写」打散,每次 write 仍是完整的 —— 实测没加锁的实现照样 0 坏行,
    这条测试就白写了。劈成两半、中间让出 GIL,别的线程才插得进来。

    实测(4 线程 x 200 行,各跑 10 轮):真 Out 0 坏行 10/10,
    拆掉锁 6 坏行 10/10。
    """

    def write(self, s):
        half = len(s) // 2
        if half:
            super().write(s[:half])
            time.sleep(0)
            return half + super().write(s[half:])
        return super().write(s)


def test_out_is_thread_safe() -> str:
    """四条线程同时写,行不能被撕成两半。

    用 _Yielding 而不是 StringIO:真 StringIO 的 write 在 GIL 下是原子的,
    有没有锁都过 —— 这条测试就白写了。
    """
    buf = _Yielding()
    out = bridge.Out(buf)

    def spam(n: int) -> None:
        for i in range(200):
            out.event("log", level="info", text=f"t{n}-{i}")

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = buf.getvalue().splitlines()
    assert len(lines) == 800, f"丢了行:{len(lines)}"
    for ln in lines:                      # 每一行都必须是完整 JSON
        json.loads(ln)
    return "  多线程写入不撕行(800 行全是完整 JSON)  ✓"


def test_event_sink_splits_and_flushes() -> str:
    """_EventSink 的 chunk 边界和 done 标记。

    三条 cli 测试都走 rid=None 那条路,这条直接测事件流那一侧。
    """
    out = bridge.Out(io.StringIO())
    sink = bridge._EventSink(out, 7, io.StringIO())

    sink.write("a\nb")           # 只有一整行该出去
    evs = _lines(out)
    assert len(evs) == 1, evs
    assert evs[0] == {"event": "output", "id": 7, "chunk": "a\n", "done": False}, evs

    sink.flush()                 # 残尾:发出去,但 done 还是 False
    evs = _lines(out)
    assert len(evs) == 2, evs
    assert evs[1]["chunk"] == "b" and evs[1]["done"] is False, evs[1]
    assert sum(1 for e in evs if e["done"]) == 0, "sink 自己不该发 done=True"

    sink.flush()                 # 再 flush 一次不该多发
    assert len(_lines(out)) == 2
    return "  _EventSink:按行切 chunk,flush 发残尾且 done 唯一  ✓"


# ------------------------------------------------------------------- 夹具

def _cfg(root: Path):
    """和 test_loop 同款。bridge 只用得到其中一部分字段。"""
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        root=root,
        workspace=root / "workspace",
        db=root / "dot.db",
        tick_seconds=1,
        model=SimpleNamespace(name="fake:model", base_url=None, max_tokens=64,
                              temperature=0.0),
        smtp=SimpleNamespace(usable=False, host="", port=587, user="", sender="",
                             to="", password_env="X", use_tls=True, password=""),
        sources=SimpleNamespace(
            fs=SimpleNamespace(enabled=True, paths=["inbox"], interval=60),
            imap=SimpleNamespace(enabled=False, host="", port=993, user="",
                                 folder="INBOX", password_env="X", interval=60,
                                 password=""),
            ical=SimpleNamespace(enabled=False, url="", interval=60),
        ),
        rules={"send_email": "ask", "run_shell": "deny", "save_skill": "allow"},
        approval_subject_prefix="[dot#",
        approval_timeout_hours=72,
        daily_token_budget=0,
        bots={"dot": SimpleNamespace(name="dot", prompt="", tools=[])},
        skills=SimpleNamespace(enabled=True, dir="skills"),
        handoff=SimpleNamespace(max_hops=2),
    )
    cfg.bot = lambda name: cfg.bots.get(name) or SimpleNamespace(
        name=name, prompt="", tools=[]
    )
    return cfg


def _setup(tmp: Path):
    """造一个临时工作区 + 一份真的 dot.toml。

    dot.toml 是必须的:bridge 跑 CLI 命令时会带上 -c 指向它。不写的话
    命令会去找开发机 ~/.dots/dot.toml —— 测试就变成看运气了。
    """
    cfg = _cfg(tmp)
    (cfg.workspace / "inbox").mkdir(parents=True, exist_ok=True)
    toml = tmp / "dot.toml"
    toml.write_text(
        '[dot]\nworkspace = "workspace"\ndb = "dot.db"\n'
        '[model]\nname = "fake:model"\n',
        encoding="utf-8",
    )
    conn = db.connect(cfg.db)
    db.init(conn)
    conn.close()
    out = bridge.Out(io.StringIO())
    return cfg, bridge.Bridge(cfg, out=out, config_path=toml), out


def _lines(out) -> list[dict]:
    return [json.loads(x) for x in out._stream.getvalue().splitlines()]


class _State:
    def __init__(self, messages=None, next_=()):
        self.values = {"messages": list(messages or [])}
        self.next = tuple(next_)


class StubAgent:
    """按脚本回应。script 每一项是一次 invoke 的返回值。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list = []
        self._states: dict = {}

    def get_state(self, config):
        return self._states.get(config["configurable"]["thread_id"], _State())

    def invoke(self, payload, config):
        tid = config["configurable"]["thread_id"]
        self.calls.append((tid, payload))
        result = self.script.pop(0) if self.script else {"messages": []}
        self._states[tid] = _State(result.get("messages"))
        return result


def _ai(text: str, tokens: int = 0):
    from langchain_core.messages import AIMessage

    msg = AIMessage(content=text)
    if tokens:
        msg.usage_metadata = {"input_tokens": tokens, "output_tokens": 0,
                              "total_tokens": tokens}
    return msg


def _open(cfg):
    conn = db.connect(cfg.db)
    db.init(conn)
    return conn


def _wait_for(pred, timeout: float = 10.0, interval: float = 0.05) -> None:
    """等一个条件成立。比 sleep 固定秒数稳,也不会拖慢通过的情况。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError(f"等了 {timeout}s 条件还是不成立")


class _Interrupt:
    def __init__(self, value):
        self.value = value


class _NoSaver:
    class conn:                      # noqa: N801 —— 只为了 .close() 能调
        @staticmethod
        def close():
            pass


def test_handle_state_empty() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        b.handle({"id": 1, "cmd": "state"})

        msg = _lines(out)[-1]
        assert msg["id"] == 1 and msg["ok"] is True, msg
        data = msg["data"]
        assert data["goals"] == [] and data["approvals"] == [], data
        assert data["audit"] == [], data
        assert data["skills"] == [], data
    return "  state 返回三张表的全量快照(外加技能面板要的 skills)  ✓"


def test_handle_goal() -> str:
    """下目标:落库、origin 标成 gui、状态直接 ready(界面下的目标 = 人授权的)。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        b.handle({"id": 2, "cmd": "goal", "text": "把 inbox 整理成报告"})

        # 按 id 找应答,不看行序 —— 快照是 push_state 另推的,
        # 两者谁先谁后是实现细节,不该由测试钉死。
        msg = [m for m in _lines(out) if m.get("id") == 2][-1]
        assert msg["ok"] is True, msg
        gid = msg["data"]["gid"]

        conn = db.connect(cfg.db)
        try:
            row = db.get_goal(conn, gid)
        finally:
            conn.close()
        assert row["text"] == "把 inbox 整理成报告"
        assert row["status"] == db.READY, row["status"]
        assert row["origin"] == "gui", row["origin"]

        # 落库之后要主动推一次快照,界面不用自己再来问
        events = [m for m in _lines(out) if m.get("event") == "state"]
        assert events, "下完目标没推快照"
        assert len(events[-1]["goals"]) == 1
    return "  goal 落库 + origin=gui + 自动推快照  ✓"


def test_handle_bad_input() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        b.handle({"id": 3, "cmd": "没有这个命令"})
        b.handle({"id": 4, "cmd": "goal", "text": "   "})
        b.handle({"id": 5, "cmd": "goal", "text": "x", "bot": "nope"})

        msgs = _lines(out)
        assert msgs[0]["ok"] is False and "不认识" in msgs[0]["error"], msgs[0]
        assert msgs[1]["ok"] is False and "空" in msgs[1]["error"], msgs[1]
        assert msgs[2]["ok"] is False and "没有叫" in msgs[2]["error"], msgs[2]
    return "  未知命令 / 空目标 / 不存在的 bot 都报错而不是崩  ✓"


def test_cli_channel_captures_output() -> str:
    """跑一条 CLI 命令,把 stdout 和退出码拿回来。

    不逐个实现命令,而是复用 cli 自己那份命令表 —— 以后 dots 加子命令,
    界面加个按钮就行,两边都不用改协议。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        code, text = b.run_cli(["cost"])

        assert code == 0, (code, text)
        assert "今日" in text, text
    return "  cli 通道:跑命令、抓输出、拿退出码  ✓"


def test_cli_channel_refuses_blocking_commands() -> str:
    """serve 和空 argv 必须拒掉。

    serve 是常驻循环 —— bridge 自己就是那个循环,再跑一个就是两个 worker
    抢同一个库。空 argv 会掉进交互终端,那个会阻塞读 input(),在子进程里
    就是挂死。

    带顶层选项的写法也得拒:`dots -c x serve` 和 `dots -c x` 都是合法命令行,
    只认 argv[0] 的话两种都能从闸门底下钻过去。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        for argv in (["serve"], [], ["serve", "--web"], ["-c", "x", "serve"],
                     ["--config=x", "serve"], ["-c", "x"], ["--config=x"]):
            code, text = b.run_cli(argv)
            assert code == 2, (argv, code, text)
            assert "不能" in text or "拒" in text, text

        # 反向:这些不是「serve」,闸门不能误拒。这里直接问闸门,不跑命令 ——
        # 带 `-c x` 的命令真跑起来会去找那份不存在的配置,那是另一回事。
        for argv in (["goal", "serve", "this"], ["ls"], ["-c", "x", "ls"],
                     ["tick"], ["-c", "x", "tick"]):
            assert bridge._cli_argv_ok(argv) is None, argv
    return "  serve / 空 argv / 带 -c 的写法都被拒,别的命令不误伤  ✓"


def test_cli_channel_reports_argparse_errors() -> str:
    """argparse 打错参数会 sys.exit(2),得当成普通结果回传,不能把进程带走。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        code, text = b.run_cli(["log", "--nope"])
        assert code == 2, (code, text)
        assert "nope" in text or "usage" in text.lower(), text
    return "  argparse 报错被接住,退出码透传  ✓"


def test_cli_channel_streams_output_events() -> str:
    """带 rid 跑命令:输出要按行变成 output 事件,并且有收尾。

    这条是 Task 8 命令线程的真实路径,之前只有 rid=None 那条被测过。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        code, text = b.run_cli(["cost"], rid=7)

        assert code == 0 and "今日" in text, (code, text)
        evs = [m for m in _lines(out) if m.get("event") == "output"]
        assert evs, "一条 output 事件都没有"
        assert all(e["id"] == 7 for e in evs), evs
        # done=True 必须**恰好一条** —— 发两条的话界面会把「还在跑」误判成跑完
        assert sum(1 for e in evs if e["done"]) == 1, evs
    return "  run_cli(rid):输出按行推成事件,done 收尾恰好一条  ✓"


def test_loop_start_and_stop() -> str:
    """启停 tick 循环,并且它在真跑 worker.tick。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([{"messages": [_ai("处理完了", tokens=10)]}] * 5)
        b = bridge.Bridge(cfg, out=out, config_path=cfg.root / "dot.toml",
                          make_agents=lambda conn: (agent, _NoSaver()))

        assert b.loop_running() is False
        b.handle({"id": 1, "cmd": "loop", "action": "start"})
        _wait_for(lambda: b._loop_thread and b._loop_thread.is_alive())
        assert b.loop_running() is True

        # 往 inbox 扔个文件,循环应该自己发现并跑掉
        (cfg.workspace / "inbox" / "a.txt").write_text("hello", encoding="utf-8")

        def has_goal() -> bool:
            conn = _open(cfg)
            try:
                return bool(db.list_goals(conn))
            finally:
                conn.close()

        _wait_for(has_goal, timeout=15)

        # 循环跑着的时候,手动 tick 要被服务端拦下 —— 带 -c 的写法也一样
        for argv in (["tick"], ["-c", str(cfg.root / "dot.toml"), "tick"]):
            code, text = b.run_cli(argv)
            assert code == 2 and "已经有 tick 在跑" in text, (argv, code, text)

        b.handle({"id": 2, "cmd": "loop", "action": "stop"})
        _wait_for(lambda: not b.loop_running(), timeout=5)
        assert b.loop_running() is False
        _wait_for(lambda: not (b._loop_thread and b._loop_thread.is_alive()), timeout=5)
        b.shutdown()          # 断言提前挂了也要收干净,不然线程攥着 dot.db,
                              # 临时目录删不掉,报出来的是 WinError 32 而不是真错
    return "  循环能起、能发现文件事件、能停,跑着时手动 tick 被拦  ✓"


def test_quit_puts_running_goal_back_to_ready() -> str:
    """正常退出时 running 的目标放回 ready。

    recover_stale_runs 的阈值是 1800 秒。正常关个窗口不该让一个跑到一半的
    目标白等半小时。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        conn = _open(cfg)
        try:
            gid = db.add_goal(conn, "跑到一半关了窗口", origin="cli", status=db.RUNNING)
        finally:
            conn.close()

        b.shutdown()

        conn = _open(cfg)
        try:
            row = db.get_goal(conn, gid)
            assert row["status"] == db.READY, row["status"]
            kinds = {r["kind"] for r in db.recent_audit(conn, 20)}
            assert "bridge_shutdown" in kinds, kinds
        finally:
            conn.close()
    return "  退出时 running 的目标被放回 ready,并记进 audit  ✓"


def test_chat_returns_reply() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([{"messages": [_ai("inbox 里有 3 个文件", tokens=42)]}])
        b = bridge.Bridge(cfg, out=out, config_path=cfg.root / "dot.toml",
                          make_chat=lambda conn: (agent, _NoSaver()))

        b.start_chat()
        b.handle({"id": 1, "cmd": "chat", "text": "工作区里有什么?"})
        _wait_for(lambda: any(m.get("event") == "chat" for m in _lines(out)))

        # 先有回执:界面按 id 配对,不回执它那个 promise 永远不 resolve
        acks = [m for m in _lines(out) if m.get("id") == 1 and "ok" in m]
        assert acks and acks[-1]["data"] == {"received": True, "queued": True}, acks

        events = [m for m in _lines(out) if m.get("event") == "chat"]
        assert events[-1]["text"] == "inbox 里有 3 个文件", events[-1]
        assert events[-1]["tokens"] == 42, events[-1]
        assert events[-1]["id"] == 1, "chat 事件没带 id,界面配不上对"
        starts = [m for m in _lines(out) if m.get("event") == "chat_start"]
        assert starts and starts[-1]["id"] == 1, "没推 chat_start(或没带 id)"
        b.shutdown()
    return "  聊天:回执 -> chat_start -> 回答,token 和 id 都对  ✓"


def test_chat_ask_roundtrip() -> str:
    """聊天里碰到 ask 类动作:推 ask 事件,阻塞等界面回话。

    这条是四条线程设计的全部理由 —— 聊天跑在 chat 线程上,所以读 stdin 的
    reader 线程还闲着,answer 才送得进来。反过来就会死锁。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([
            {"__interrupt__": (_Interrupt(
                {"action": "send_email", "args": {"to": "alice@example.com"}}),)},
            {"messages": [_ai("邮件已发")]},
        ])
        b = bridge.Bridge(cfg, out=out, config_path=cfg.root / "dot.toml",
                          make_chat=lambda conn: (agent, _NoSaver()))
        b.start_chat()
        b.handle({"id": 1, "cmd": "chat", "text": "给 alice 发封邮件"})

        # 等 ask 事件冒出来
        _wait_for(lambda: any(m.get("event") == "ask" for m in _lines(out)))
        ask = [m for m in _lines(out) if m.get("event") == "ask"][-1]
        assert ask["action"] == "send_email", ask
        assert ask["args"]["to"] == "alice@example.com", ask

        # 界面把它当成另一条 stdin 行送回来 —— 就是真实路径
        b.handle({"id": 2, "cmd": "answer", "qid": ask["qid"], "value": True})

        _wait_for(lambda: any(m.get("event") == "chat" for m in _lines(out)))
        reply = [m for m in _lines(out) if m.get("event") == "chat"][-1]
        assert reply["text"] == "邮件已发", reply
        assert reply["id"] == 1, "chat 事件没带 id"
        # 第二次 invoke 必须带 resume={"approved": True}
        assert agent.calls[1][1].resume == {"approved": True}, agent.calls[1][1]
        b.shutdown()
    return "  聊天里的审批:推 ask -> 界面回 answer -> 带 approved 续跑  ✓"


def test_chat_ask_timeout_denies() -> str:
    """没人回话就按拒绝处理。一个永远等不到回复的审批 = 一个永远卡住的目标。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([
            {"__interrupt__": (_Interrupt({"action": "run_shell", "args": {}}),)},
            {"messages": [_ai("好吧,那我不跑了")]},
        ])
        b = bridge.Bridge(cfg, out=out, config_path=cfg.root / "dot.toml",
                          make_chat=lambda conn: (agent, _NoSaver()))

        old = bridge.ASK_TIMEOUT
        bridge.ASK_TIMEOUT = 0.2      # 常量在模块级,得打补丁
        try:
            b.start_chat()
            b.handle({"id": 1, "cmd": "chat", "text": "跑个命令"})
            _wait_for(lambda: any(m.get("event") == "chat" and m.get("id") == 1
                                  for m in _lines(out)),
                      timeout=10)
        finally:
            bridge.ASK_TIMEOUT = old

        assert agent.calls[1][1].resume == {"approved": False}, agent.calls[1][1]
        b.shutdown()
    return "  审批等超时按拒绝处理,聊天继续往下走  ✓"


def test_cli_command_goes_through_the_command_thread() -> str:
    """命令必须异步跑:selftest 要 30 秒,不能让 reader 线程干等。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        b.start_commands()
        b.handle({"id": 5, "cmd": "cli", "argv": ["cost"]})

        # handle 立刻返回,不等命令跑完 —— 这就是异步的证据
        _wait_for(lambda: any(m.get("id") == 5 for m in _lines(out)))
        msg = [m for m in _lines(out) if m.get("id") == 5][-1]
        assert msg["ok"] is True and msg["data"]["code"] == 0, msg
        assert "今日" in msg["data"]["out"], msg
        b.shutdown()
    return "  cli 命令走 command 线程,reader 不被堵住  ✓"


def test_stdin_loop_end_to_end() -> str:
    """进程级:真起一个子进程,喂 JSON 行,读应答。

    这是「手敲 JSON 就能验证」那条验收标准的自动化版。
    """
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        cfgdir = Path(tmp)
        (cfgdir / "inbox").mkdir()
        (cfgdir / "dot.toml").write_text(
            '[dot]\nworkspace = "workspace"\ndb = "dot.db"\n'
            '[model]\nname = "fake:model"\n',
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "dots", "bridge", "--no-loop"],
            cwd=str(cfgdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env={**os.environ, "PYTHONPATH": str(root),
                 "PYTHONIOENCODING": "utf-8"},
        )
        # 看门狗:后端一卡,readline() 会无限等,这条用例（它同时也在
        # `dots selftest` 里,等于界面的「自检」按钮）就永远转圈。
        # 到点 kill,readline 拿到 EOF,断言自己会失败,不会挂死整场测试。
        watchdog = threading.Timer(90, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            first = json.loads(proc.stdout.readline())
            assert first.get("event") in ("ready", "fatal"), first

            def send(obj):
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                proc.stdin.flush()

            def read_until(pred, limit=200):
                for _ in range(limit):
                    line = proc.stdout.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                    if pred(msg):
                        return msg
                raise AssertionError("没等到要的消息")

            send({"id": 1, "cmd": "state"})
            st = read_until(lambda m: m.get("id") == 1)
            assert st["ok"] is True, st

            send({"id": 2, "cmd": "goal", "text": "整理 inbox"})
            g = read_until(lambda m: m.get("id") == 2)
            assert g["data"]["gid"], g

            send({"id": 3, "cmd": "cli", "argv": ["cost"]})
            # 要挑「应答」,不能只按 id 挑:output 事件也带同一个 id
            # (界面靠它把输出贴到对应那条命令下面),应答的标志是有 "ok"。
            c = read_until(lambda m: m.get("id") == 3 and "ok" in m)
            assert c["data"]["code"] == 0, c

            send({"id": 4, "cmd": "quit"})
            read_until(lambda m: m.get("id") == 4)
        finally:
            watchdog.cancel()
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()                  # 兜底:不等了,直接收尸
                proc.wait(timeout=5)
    return "  真起子进程:ready -> state -> goal -> cli -> quit 全通  ✓"


def test_loop_crash_is_reported_as_crashed() -> str:
    """建图/建库失败是最常见的死法(模型名写错、DB 打不开),界面得能认出来。

    用现成的注入口造故障,不碰任何模块全局。
    """
    def boom(conn):
        raise SystemExit("建不出模型 'fake:model'")

    with tempfile.TemporaryDirectory() as tmp:
        cfg, _b, out = _setup(Path(tmp))
        b = bridge.Bridge(cfg, out=out, config_path=cfg.root / "dot.toml",
                          make_agents=boom)
        b.handle({"id": 1, "cmd": "loop", "action": "start"})

        _wait_for(lambda: any(m.get("event") == "loop_stopped" for m in _lines(out)))
        fatal = [m for m in _lines(out) if m.get("event") == "fatal"]
        assert fatal and fatal[-1]["where"] == "tick", _lines(out)
        stopped = [m for m in _lines(out) if m.get("event") == "loop_stopped"][-1]
        assert stopped["reason"] == "crashed", stopped
        b.shutdown()
    return "  建图失败:报 fatal + loop_stopped(crashed),不是静默死掉  ✓"


def test_shutdown_survives_a_broken_db() -> str:
    """退出路径上不能再抛。库打不开时只能 warn,不能把「干净退出」变成一屏栈。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        # 让 db 指向一个目录 —— sqlite 打开目录必抛
        (cfg.root / "not-a-db").mkdir()
        cfg.db = cfg.root / "not-a-db"

        b.shutdown()          # 不该抛

        warns = [m for m in _lines(out)
                 if m.get("event") == "log" and m.get("level") == "warn"]
        assert warns, "复位失败既没抛也没说,静默丢了"
    return "  库坏掉时 shutdown 只 warn 不抛,退出路径干净  ✓"


def run() -> str:
    cases = [
        test_current_is_thread_local,
        test_use_or_create_is_per_thread,
        test_light_commands_do_not_import_langchain,
        test_out_writes_one_json_line,
        test_out_reply_shape,
        test_out_is_thread_safe,
        test_event_sink_splits_and_flushes,
        test_handle_state_empty,
        test_handle_goal,
        test_handle_bad_input,
        test_cli_channel_captures_output,
        test_cli_channel_refuses_blocking_commands,
        test_cli_channel_reports_argparse_errors,
        test_cli_channel_streams_output_events,
        test_loop_start_and_stop,
        test_quit_puts_running_goal_back_to_ready,
        test_chat_returns_reply,
        test_chat_ask_roundtrip,
        test_chat_ask_timeout_denies,
        test_cli_command_goes_through_the_command_thread,
        test_stdin_loop_end_to_end,
        test_loop_crash_is_reported_as_crashed,
        test_shutdown_survives_a_broken_db,
    ]
    notes = []
    for case in cases:
        notes.append(case())
    return "\n".join(notes)


def test_all_bridge():
    run()


if __name__ == "__main__":
    print(run())
