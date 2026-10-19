"""桌面界面的后端:stdin/stdout 上一行一个 JSON。

界面(Electron)开一个常驻进程跑这个,把「建图」的成本只付一次 ——
本机 conda 环境里 `from dots import cli` 要 8.7 秒(langchain 顺手
import transformers→torch),点一下按钮现起一个进程是不能接受的。

四条线程,各管一摊,互不阻塞:
  reader(主)  独占读 stdin 并分发。聊天中途要弹审批确认时必须有别的线程
              在读 stdin,否则死锁 —— 这是它单独一条的全部理由。
  tick        worker.tick() 循环。一个目标可能跑几分钟。
  chat        worker.chat_send()。用户打字不该等 tick。
  command     cli.main(argv)。selftest 要 30 秒。

这里只有协议胶水,不重写任何业务逻辑 —— 全部转手给 worker / db / cli。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import queue
import sys
import threading
import time
import traceback

from dots import __version__
from dots import db
from dots import config as config_mod
from dots import graph
from dots import rules as rules_mod
from dots import skills as skills_mod
from dots import worker

# 聊天里那个审批确认最多等多久(秒)。超时按拒绝处理 —— 失败要往安全的方向倒。
ASK_TIMEOUT = 600


class Out:
    """线程安全的 stdout 写入。

    多线程同时写会把 JSON 行撕成两半,对面按行读就直接解析失败。
    所有写入走同一把锁。

    构造时就把 stream 抓住:跑 CLI 命令时会把 sys.stdout 换成事件流,
    那时候我们还必须写到真 stdout 上。
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def raw(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def reply(self, rid, data=None, error=None) -> None:
        if error is None:
            self.raw({"id": rid, "ok": True, "data": data if data is not None else {}})
        else:
            self.raw({"id": rid, "ok": False, "error": str(error)})

    def event(self, name: str, **fields) -> None:
        self.raw({"event": name, **fields})


def snapshot(cfg) -> dict:
    """三张表的全量快照:目标、待批、最近活动。

    每次开一条新连接、用完就关 —— 跟 web.py 一个路子。这样任何线程都能
    推状态,不用操心「哪条连接归哪个线程」。
    """
    conn = db.connect(cfg.db)
    try:
        return {
            "goals": [dict(r) for r in db.list_goals(conn, limit=60)],
            "approvals": [dict(r) for r in db.pending_approvals(conn)],
            "audit": [dict(r) for r in db.recent_audit(conn, 60)],
            # 技能面板要有结构化来源:否则界面只能去解析 cli 打的人读表格
            # (那个还按东亚宽度对齐过)。
            "skills": (
                [{"name": s.name, "description": s.description}
                 for s in skills_mod.scan(cfg)]
                if cfg.skills.enabled else []
            ),
        }
    finally:
        conn.close()


# 这些命令不能从界面跑:
#   serve / bridge —— 常驻循环由 bridge 自己管,再起一个就是两个 worker 抢
#                    同一个库;嵌套 bridge 还会去抢 stdin
#   空 argv —— 会掉进交互终端,那个阻塞在 input() 上,在子进程里就是挂死
_REFUSED = {"serve", "bridge"}


def _cli_head(argv: list) -> str:
    """取子命令名,跳掉 -c/--config 这类顶层选项。

    只看 argv[0] 是不够的:`dots -c x serve` 和 `dots -c x tick` 都能从
    闸门底下钻过去 —— 前者是「两个 worker 抢同一个库」,后者会让
    `_cmd_repl` 去读 bridge 的 stdin 并永远不返回。
    """
    words = [str(x) for x in argv]
    i = 0
    while i < len(words):
        w = words[i]
        if not w.startswith("-"):
            return w
        if w in ("-c", "--config"):
            i += 2              # 选项和它的值
        else:
            i += 1              # -cPATH / --config=PATH / 其它开关
    return ""


def _cli_argv_ok(argv: list) -> str | None:
    """能跑返回 None,不能跑返回一句给界面看的话。"""
    head = _cli_head(argv)
    if not head:
        return "不能跑:没给命令。会掉进交互终端,那个要真人在终端前面。"
    if head in _REFUSED:
        return (
            f"不能跑 {head!r}:常驻循环由界面自己管(顶栏那个启动/停止)。"
            f"再起一个就是两个 worker 抢同一个库。"
        )
    return None


class _EventSink(io.TextIOBase):
    """把 print 变成一行行 output 事件,同时留一份在 buf 里。

    要装得像真 stdout:print() 会调 write(),有些库会问 isatty() / encoding。

    chunk 的切法:写进来的字节按「整行」切,每整行一条 done=False 的事件;
    没有换行结尾的那截留在 _pending 里,等 flush() 才作为残尾发出去
    (done 仍是 False)。**done=True 只有 run_cli 收尾那一条** —— 它是界面
    「这条命令跑完了」的唯一信号,多一条就会把还在跑的命令误判成跑完。
    """

    # io.TextIOBase.encoding 是 None,而有些库上来就 .encoding.lower()
    encoding = "utf-8"

    def __init__(self, out: Out, rid, buf: io.StringIO) -> None:
        self._out = out
        self._rid = rid
        self._buf = buf
        self._pending = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, s) -> int:
        s = str(s)
        self._buf.write(s)
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._out.event("output", id=self._rid, chunk=line + "\n", done=False)
        return len(s)

    def flush(self) -> None:
        if self._pending:
            # 残尾按普通 chunk 发,done 仍是 False —— done=True 只有 run_cli
            # 收尾那一条。界面「转圈结束」的信号必须唯一,不能发两条。
            self._out.event("output", id=self._rid, chunk=self._pending, done=False)
            self._pending = ""


class Bridge:
    """协议分发 + 三条后台线程的持有者。

    make_agents / make_chat 是注入口:测试传假图,生产走 worker 那套。
    """

    def __init__(self, cfg, *, out=None, config_path=None,
                 make_agents=None, make_chat=None) -> None:
        self.cfg = cfg
        self.out = out if out is not None else Out()
        # 每一条 CLI 命令都要带上同一份 -c,否则界面里的「列目标」可能读的是
        # 另一份 dot.toml,和 bridge 自己用的那份不是同一个库。
        self.config_path = config_path
        # worker.build_agents 的签名是 (cfg, conn),这里统一收成
        # (conn) -> (agents, saver)。两条线程都得能关掉 checkpointer ——
        # 它自己攥着一条 sqlite 连接,不关的话 Windows 上文件一直被锁着
        # (worker.py 里为这个踩过两次)。
        self._make_agents = make_agents or (lambda conn: worker.build_agents(cfg, conn))
        self._make_chat = make_chat or (lambda conn: worker.chat_agents(cfg, conn))

        self._quit = threading.Event()
        self._stop = threading.Event()        # 整个进程退出
        self._loop_stop = threading.Event()   # 只停 tick 循环
        # 谁在跑 tick(长驻循环或手动一轮)。只有一把 —— 两个 tick 主人
        # 会双写同一份 checkpoint,还会让 _suppress_self_writes 失效
        # (agent 自产的文件被另一个循环当成新事件收进来)。
        self._tick_lock = threading.Lock()
        self._lock = threading.Lock()
        self._qid = 0
        self._waiting: dict[str, threading.Event] = {}
        self._answers: dict[str, bool] = {}

        self._chat_q: queue.Queue = queue.Queue()
        self._cmd_q: queue.Queue = queue.Queue()
        self._loop_thread: threading.Thread | None = None
        self._chat_thread: threading.Thread | None = None
        self._cmd_thread: threading.Thread | None = None

    # ------------------------------------------------------------- 分发

    def handle(self, msg: dict) -> None:
        """处理一条命令。应答直接写出去,所以不返回东西。"""
        rid = msg.get("id")
        cmd = msg.get("cmd")
        try:
            if cmd == "state":
                self.out.reply(rid, snapshot(self.cfg))
            elif cmd == "goal":
                self._do_goal(rid, msg)
            elif cmd == "loop":
                self._do_loop(rid, msg)
            elif cmd == "chat":
                # 懒启动:建对话图要几秒,不聊天就不付这个成本
                self.start_chat()
                # 先回执再入队:界面按 id 配对请求/应答,不回执它那个
                # promise 就永不 resolve。真正的答案走 chat 事件(带 id)。
                self.out.reply(rid, {"received": True, "queued": True})
                self._chat_q.put(msg)          # 丢给 chat 线程,不能堵住 reader
            elif cmd == "answer":
                # 每条命令都有应答,这条也不例外 —— 界面按 id 配对的话,
                # 不回它就会留一个永远 pending 的 promise。
                # received=False 说明这个 qid 已经过期(等超时了),界面得说实话。
                self.out.reply(rid, {"received": self._deliver_answer(msg)})
            elif cmd == "cli":
                # 丢给命令线程就跑 —— 命令可能跑 30 秒(selftest),
                # reader 还得能接着收 quit。
                self.start_commands()   # 线程意外死了要能自愈,不然之后每条命令都石沉大海
                self._cmd_q.put(msg)
            elif cmd == "quit":
                self.out.reply(rid, {"bye": True})
                self._quit.set()
            else:
                self.out.reply(rid, error=f"不认识这条命令:{cmd!r}")
        except Exception as e:
            # 分发不能把后端带崩,但也不能让 bug 只留下一句字符串 ——
            # 栈单独走 log 事件,和 worker.serve 一个路子。
            self.out.event("log", level="error", text=traceback.format_exc()[-600:])
            self.out.reply(rid, error=f"{type(e).__name__}: {e}")

    def _do_goal(self, rid, msg) -> None:
        text = str(msg.get("text") or "").strip()
        if not text:
            self.out.reply(rid, error="目标不能是空的。")
            return
        bot = str(msg.get("bot") or "dot").strip().lower()
        if bot not in self.cfg.bots:
            self.out.reply(
                rid, error=f"没有叫 {bot!r} 的 bot。可用的:{', '.join(sorted(self.cfg.bots))}"
            )
            return
        conn = db.connect(self.cfg.db)
        try:
            # 外部事件直接 ready:你已经把它写进界面了,那就是授权。
            # agent 自己派生出来的目标才需要人工批。
            gid = db.add_goal(conn, text, origin="gui", status=db.READY, bot=bot)
            db.audit(conn, gid, "goal_created", f"[{bot}] {text[:200]}")
        finally:
            conn.close()
        # 先推快照再回应答:界面拿到 id 时会顺手去列表里找这条新目标,
        # 快照没出去就找不到。应答放最后,它就一定是「一切都已经好了」。
        self.push_state()
        self.out.reply(rid, {"gid": gid})

    def push_state(self) -> None:
        self.out.event("state", **snapshot(self.cfg))

    # ----------------------------------------------------------- CLI 通道

    def run_cli(self, argv: list, rid=None) -> tuple[int, str]:
        """跑一条 CLI 命令,返回 (退出码, 输出)。

        复用 cli 自己那一份命令表:按钮 = 一条 argv,零重复。以后 dots 加
        子命令,界面加个按钮即可,协议不用动。

        输出边跑边以 output 事件推出去(selftest 要 30 秒,不能等跑完),
        最后再整份回一次,界面就算漏了事件也能显示全。
        """
        from dots import cli as cli_mod      # 惰性:bridge 自己不需要它

        problem = _cli_argv_ok(argv)
        if problem:
            return 2, problem

        argv = [str(x) for x in argv]
        # 手动一轮也要占 tick 归属:光看 loop_running 是 check-then-act ——
        # 这一轮的图还没建完(真模型下几十秒)、循环还没起来,那个窗口里
        # 界面点「开始值守」就能挤进第二个 worker。
        manual_tick = _cli_head(argv) == "tick"
        if manual_tick and not self._tick_lock.acquire(blocking=False):
            return 2, (
                "不能跑 tick:已经有 tick 在跑(长驻循环,或另一轮手动 tick)。"
                "两个同时跑就是抢同一个库。"
            )
        try:
            if self.config_path is not None:
                # 钉住配置:不带 -c 的话,命令会去 CWD 找 ./dot.toml,
                # 界面里点「列目标」可能读出另一个库的数据。
                argv = ["-c", str(self.config_path), *argv]
            buf = io.StringIO()
            sink = _EventSink(self.out, rid, buf) if rid is not None else buf

            code = 0
            try:
                # 注意:sys.stdout/stderr 是进程全局的。命令线程跑的时候,
                # tick 线程的 print 也会被一起收进来 —— 罕见,先接受。
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    code = cli_mod.main(argv)
            except SystemExit as e:          # argparse 报错 / sys.exit
                code = e.code if isinstance(e.code, int) else 1
                if not isinstance(e.code, int) and e.code:
                    # 字符串那半是给人看的文案(比如「建不出模型…检查 [model] 段」),
                    # 丢了的话界面点完按钮只有「退出码 1 + 空白面板」。
                    print(e.code, file=sink)
            except Exception:
                code = 1
                traceback.print_exc(file=sink)
            finally:
                # 必须显式 flush:不调的话那一截没有换行结尾的输出永远不出门,
                # 而且 GC/解释器退出时 __del__ → close() → flush() 会在 json
                # 已被拆掉的时机跑,甩一屏栈。
                sink.flush()
                if rid is not None:
                    # 唯一的那条收尾事件:界面靠它把「转圈」停掉。正常命令的
                    # print 都带 \n,flush 什么也不发,所以不能指望 flush 来收尾。
                    self.out.event("output", id=rid, chunk="", done=True)

            return int(code), buf.getvalue()
        finally:
            if manual_tick:
                self._tick_lock.release()

    def _run_cli_locked(self, argv: list, rid) -> tuple[int, str]:
        """run_cli 的主体,进来时 argv 已经钉好、tick 归属已经拿好。"""
        from dots import cli as cli_mod

        if self.config_path is not None:
            # 钉住配置:不带 -c 的话,命令会去 CWD 找 ./dot.toml,
            # 界面里点「列目标」可能读出另一个库的数据。
            argv = ["-c", str(self.config_path), *argv]
        buf = io.StringIO()
        sink = _EventSink(self.out, rid, buf) if rid is not None else buf

        code = 0
        try:
            # 注意:sys.stdout/stderr 是进程全局的。命令线程跑的时候,
            # tick 线程的 print 也会被一起收进来 —— 罕见,先接受。
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                code = cli_mod.main(argv)
        except SystemExit as e:              # argparse 报错 / sys.exit
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                # 字符串那半是给人看的文案(比如「建不出模型…检查 [model] 段」),
                # 丢了的话界面点完按钮只有「退出码 1 + 空白面板」。
                print(e.code, file=sink)
        except Exception:
            code = 1
            traceback.print_exc(file=sink)
        finally:
            # 必须显式 flush:不调的话那一截没有换行结尾的输出永远不出门,
            # 而且 GC/解释器退出时 __del__ → close() → flush() 会在 json
            # 已被拆掉的时机跑,甩一屏栈。
            sink.flush()
            if rid is not None:
                # 唯一的那条收尾事件:界面靠它把「转圈」停掉。正常命令的
                # print 都带 \n,flush 什么也不发,所以不能指望 flush 来收尾。
                self.out.event("output", id=rid, chunk="", done=True)

        return int(code), buf.getvalue()

    # ----------------------------------------------------------- tick 循环

    def loop_running(self) -> bool:
        t = self._loop_thread
        return bool(t and t.is_alive())

    def start_loop(self) -> bool:
        """起 tick 循环。已经在跑、或手动一轮正在进行,就什么都不做。"""
        if self.loop_running():
            return False
        # 拿不到锁说明命令行那条 tick 正在跑 —— 那个窗口里再起循环,
        # 就是两个 worker 同时跑同一个库。
        if not self._tick_lock.acquire(blocking=False):
            return False
        self._loop_stop = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._tick_loop, name="dot-tick", daemon=True
        )
        self._loop_thread.start()
        return True

    def stop_loop(self) -> None:
        self._loop_stop.set()

    def _tick_loop(self) -> None:
        """一条线程的整个生命周期:建自己的图、跑循环、退出时收拾干净。

        图和 RunCtx 都在这条线程里建 —— RunCtx 是线程局部的,建在别的线程
        里,这里的 graph.current() 就找不到它,工具闭包抓的也会是别人那份。
        """
        cfg = self.cfg
        conn = None
        saver = None
        crashed = False
        try:
            try:
                # 建连接也在 try 里:DB 打不开时也得留下 fatal 事件并关干净,
                # 不能带着一条没关的连接把线程摔死。
                conn = db.connect(cfg.db)
                db.init(conn)
                graph.set_current(graph.RunCtx(cfg=cfg, conn=conn))
                agents, saver = self._make_agents(conn)
            except BaseException as e:
                # 建库、建图哪一步坏了都要有 fatal:光有 loop_stopped,
                # 界面只知道「停了」,不知道「为什么」。
                # crashed 也要置上 —— 这一条是 return 走的,没异常逃出去,
                # 但对界面来说「没人点停,它自己没了」就是崩,不是停。
                crashed = True
                self.out.event("fatal", where="tick",
                               error=f"{type(e).__name__}: {e}")
                return

            recovered = worker.recover_stale_runs(cfg, conn)
            if recovered:
                self.out.event("log", level="info",
                               text=f"收回 {recovered} 个上次没跑完的目标")

            while not self._stop.is_set() and not self._loop_stop.is_set():
                try:
                    stats = worker.tick(cfg, conn, agents)
                    if stats:
                        line = worker.summarize(stats)
                        if line:
                            self.out.event("log", level="info", text=line)
                    if self._stop.is_set() or self._loop_stop.is_set():
                        break
                    # push_state 也必须在 try 里:snapshot() 每次新开连接、
                    # 执行 PRAGMA journal_mode=WAL(要一次写锁),并发写多时
                    # SQLITE_BUSY、磁盘满、库损坏都会抛。抛一次就静默打死
                    # 整条循环 —— 界面上还显示「值守中」,agent 其实已经死了。
                    self.push_state()
                except Exception:
                    # 照抄 worker.serve:一轮坏了不能干掉整个后端。
                    # 但「报错」这件事本身也会出错 —— 让 audit 写不进去的
                    # 那个原因(SQLITE_BUSY/盘满)正好也会让它抛,所以这两句
                    # 各自兜住,不然处理块一抛整条循环还是死。
                    detail = traceback.format_exc()[-1000:]
                    try:
                        db.audit(conn, None, "tick_error", detail)
                    except Exception:
                        pass
                    try:
                        self.out.event("log", level="error", text=detail)
                    except Exception:
                        pass
                self._sleep(cfg.tick_seconds)
        except BaseException:
            # 逃到这儿就是没预料到的崩。留一条 fatal 再安静退场 ——
            # 不然 Python 自己往 stderr 甩栈,那是界面看不见的地方。
            crashed = True
            try:
                self.out.event("fatal", where="tick", error=traceback.format_exc()[-600:])
            except Exception:
                pass
        finally:
            if conn is not None:
                conn.close()
            # checkpointer 自己攥着一条 sqlite 连接,不关的话 Windows 上
            # 文件一直被锁着(worker.py 里为这个踩过两次)
            if saver is not None:
                try:
                    saver.conn.close()
                except Exception:
                    pass
            # 放开 tick 归属:不放开的话停了之后就再也起不来了
            try:
                self._tick_lock.release()
            except RuntimeError:      # 没拿着(理论上到不了)也不能把退出带崩
                pass
            # 停下来的原因要让界面知道,不能只丢一句中文 log 让界面去解析。
            # crashed 排在最前:撞车时(崩溃和 quit 几乎同时)界面更需要知道
            # 「它崩了」—— 自己发过 quit 它是知道的,可以自己压掉这条提示;
            # 反过来把崩溃报成 quit,界面就永远不知道出过事。
            if crashed:
                reason = "crashed"
            elif self._stop.is_set():
                reason = "quit"
            else:
                reason = "stopped"
            self.out.event("loop_stopped", reason=reason)

    def _sleep(self, seconds: float) -> None:
        """可被打断的等待。固定 sleep 的话点了停止还要等一分钟才停。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.is_set() or self._loop_stop.is_set():
                return
            time.sleep(0.2)

    def _do_loop(self, rid, msg) -> None:
        action = msg.get("action")
        if action == "start":
            # 两个字段都得是真话:started 说「这次有没有起成」,
            # running 说「现在在不在跑」——启动失败时它们会不一样。
            started = self.start_loop()
            self.out.reply(rid, {"started": started, "running": self.loop_running()})
        elif action == "stop":
            self.stop_loop()
            # 回真实值,不回乐观值:线程可能还卡在一轮 tick 里,
            # 这时候回 {"running": False} 就是在骗界面。真退出了会另推 loop_stopped。
            self.out.reply(rid, {"running": self.loop_running()})
        else:
            self.out.reply(rid, error=f"loop 的 action 只能是 start 或 stop,收到 {action!r}")

    # --------------------------------------------------------------- 聊天

    def start_chat(self) -> bool:
        if self._chat_thread and self._chat_thread.is_alive():
            return False
        self._chat_thread = threading.Thread(
            target=self._chat_loop, name="dot-chat", daemon=True
        )
        self._chat_thread.start()
        return True

    def _chat_loop(self) -> None:
        cfg = self.cfg
        conn = None
        saver = None
        try:
            try:
                # 和 tick 那条一样,建连接也必须在 try 里:chat 这条尤其要,
                # 它失败时界面上只有一句「正在准备对话…」,不推 fatal 的话
                # 看起来像还在建图,其实线程早没了。
                conn = db.connect(cfg.db)
                db.init(conn)
                graph.set_current(graph.RunCtx(cfg=cfg, conn=conn))
                self.out.event("log", level="info", text="正在准备对话(建图要几秒)…")
                agents, saver = self._make_chat(conn)
            except BaseException as e:
                self.out.event("fatal", where="chat",
                               error=f"{type(e).__name__}: {e}")
                return
            while not self._stop.is_set():
                try:
                    msg = self._chat_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                self._do_chat(agents, conn, msg)
        finally:
            if conn is not None:
                conn.close()
            # checkpointer 自己攥着一条 sqlite 连接,不关的话 Windows 上
            # 文件一直被锁着(worker.py 里为这个踩过两次)
            if saver is not None:
                try:
                    saver.conn.close()
                except Exception:
                    pass

    def _do_chat(self, agents, conn, msg) -> None:
        # 事件带 id:界面的输入框是等「这次聊天」的答案,不带 id 它就只能
        # 靠「同一队列单消费者 FIFO」这种没写进协议的不变量去猜。
        rid = msg.get("id")
        text = str(msg.get("text") or "").strip()
        if not text:
            self.out.event("chat", id=rid, text="(空消息,没发出去)")
            return
        self.out.event("chat_start", id=rid, text=text)
        try:
            reply, used = worker.chat_send(agents, self.cfg, conn, text, self._ask)
        except Exception as e:
            detail = traceback.format_exc()[-600:]
            self.out.event("chat", id=rid, text=f"{type(e).__name__}: {e}", error=True)
            self.out.event("log", level="error", text=detail)
            return
        self.out.event("chat", id=rid, text=reply, tokens=used)
        try:
            self.push_state()
        except Exception:
            self.out.event("log", level="error", text=traceback.format_exc()[-600:])

    # ------------------------------------------------------ 聊天里的审批

    def _ask(self, payload: dict) -> bool:
        """聊天里碰到 ask 动作:推给界面,在这儿等它回话。

        这条跑在 chat 线程上,读 stdin 的是 reader 线程 —— 所以能安心阻塞。
        在 reader 线程上跑聊天就会死锁,这是四条线程设计的全部理由。
        """
        qid = self._next_qid()
        ev = threading.Event()
        with self._lock:
            self._waiting[qid] = ev
        self.out.event(
            "ask", qid=qid,
            action=payload.get("action"),
            args=payload.get("args") or {},
        )
        got = ev.wait(timeout=ASK_TIMEOUT)
        with self._lock:
            self._waiting.pop(qid, None)
            value = self._answers.pop(qid, False)
        if not got:
            self.out.event("log", level="warn",
                           text=f"审批 {qid} 等超时了({ASK_TIMEOUT}s),按拒绝处理")
            return False
        return bool(value)

    def _next_qid(self) -> str:
        with self._lock:
            self._qid += 1
            return f"q{self._qid}"

    def _deliver_answer(self, msg) -> bool:
        """把界面的决定送给正在等的 _ask。返回 False 说明这个 qid 已经过期了。"""
        qid = str(msg.get("qid") or "")
        with self._lock:
            ev = self._waiting.get(qid)
            if ev is None:
                # 陈旧的 qid(等超时了,或者拼错了):没人等它。
                # 不能假装成功 —— 界面会显示「已批准」,而 agent 早按拒绝跑了。
                return False
            self._answers[qid] = bool(msg.get("value"))
        ev.set()
        return True

    # --------------------------------------------------------- 命令线程

    def start_commands(self) -> bool:
        if self._cmd_thread and self._cmd_thread.is_alive():
            return False
        self._cmd_thread = threading.Thread(
            target=self._cmd_loop, name="dot-cmd", daemon=True
        )
        self._cmd_thread.start()
        return True

    def _cmd_loop(self) -> None:
        """串行跑 CLI 命令。一次一条 —— 同时跑两条会互相抢 sys.stdout。"""
        while not self._stop.is_set():
            try:
                msg = self._cmd_q.get(timeout=0.2)
            except queue.Empty:
                continue
            rid = msg.get("id")
            argv = msg.get("argv") or []
            if not isinstance(argv, list):
                self.out.reply(rid, error="argv 必须是数组")
                continue
            try:
                code, text = self.run_cli(argv, rid=rid)
            except Exception:
                # 命令线程不能死:它一死,之后每条命令都是石沉大海
                # (没有应答、没有事件、没有日志),界面只能一直转圈。
                detail = traceback.format_exc()[-600:]
                self.out.event("fatal", where="cmd", error=detail)
                self.out.reply(rid, error=f"命令线程出错:{detail.splitlines()[-1]}")
                continue
            self.out.reply(rid, {"code": code, "out": text})
            try:
                self.push_state()
            except Exception:
                self.out.event("log", level="error",
                               text=traceback.format_exc()[-600:])

    # ------------------------------------------------------------ 读 stdin

    def serve_stdin(self) -> int:
        """读 stdin,一行一条命令,直到 EOF 或 quit。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                self.out.raw({"ok": False, "error": f"这行不是合法 JSON:{e}"})
                continue
            if not isinstance(msg, dict):
                self.out.raw({"ok": False, "error": "一条命令必须是一个 JSON 对象"})
                continue
            self.handle(msg)
            if self._quit.is_set():
                break
        self.shutdown()
        return 0

    # --------------------------------------------------------------- 退出

    def shutdown(self) -> None:
        """优雅退出:先让线程停,再把 running 的目标放回 ready。

        顺序不能反 —— 反过来,正在跑的那一轮可能在复位之后又把状态写回去。
        join 有超时:模型调用卡住时不能让关窗口变成关不掉。
        """
        self._stop.set()
        self._loop_stop.set()
        # 必须在 join 之前:卡在 _ask 里的聊天线程,_stop 叫不醒 ev.wait(),
        # 不叫醒它就得耗满 3 秒 join 超时,而且它攥着 sqlite 连接不放,
        # Windows 上临时目录都删不掉(WinError 32,worker.py 里踩过两次)。
        # 等不到回话就是拒绝 —— 失败往安全的方向倒。
        with self._lock:
            for ev in self._waiting.values():
                ev.set()
        timed_out = []
        for name, t in (("tick", self._loop_thread), ("chat", self._chat_thread),
                        ("cmd", self._cmd_thread)):
            if t and t.is_alive():
                t.join(timeout=3.0)
                if t.is_alive():
                    timed_out.append(name)
        if timed_out:
            # 线程没停下来,复位它正在跑的目标就可能被它之后的写覆盖 ——
            # 这一笔账留给 recover_stale_runs(1800s)也行,但要说出来。
            self.out.event("log", level="warn",
                           text=f"退出时有线程没停下来:{','.join(timed_out)};"
                                f"正在跑的目标可能被重复跑一次")
        self._reset_running()

    def _reset_running(self) -> None:
        """把 running 的目标放回 ready。

        这条假定 bridge 是这台机器上唯一的 worker(所以 run_cli 拒了 serve)。
        用户同时在终端跑 `dots serve` 的话,GUI 退出会把它正在跑的目标也拽回
        ready —— 同一条目标被两个 worker 并发跑。这个假定是承重的,别悄悄去掉。
        """
        # 退出路径上不能再抛:库打不开/被占的时候,这里一抛,「干净退出」
        # 就变成非 0 退出码 + 一屏栈。复位失败顶多是目标多等一次
        # recover_stale_runs(1800s),代价小得多。
        try:
            conn = db.connect(self.cfg.db)
            try:
                for g in db.list_goals(conn, status=db.RUNNING, limit=100):
                    db.set_goal(conn, g["id"], status=db.READY)
                    db.audit(conn, g["id"], "bridge_shutdown", "界面退出,目标放回队列")
            finally:
                conn.close()
        except Exception:
            try:
                self.out.event("log", level="warn",
                               text=f"退出时复位 running 目标失败,留给下次启动回收:"
                                    f"{traceback.format_exc()[-300:]}")
            except Exception:
                pass


# ------------------------------------------------------------------ 入口

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dots bridge", description="给桌面界面用的 JSON-lines 后端(一般不用手敲)"
    )
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--no-loop", action="store_true",
                        help="起来了先不跑 tick 循环")
    args = parser.parse_args(argv)

    # Windows 上 stdin/stdout 默认是 GBK,中文 JSON 出去就是乱码。必须在
    # Bridge(cfg) 之前 —— Out 构造时就把 stdout 抓住了,晚了的话命令输出里的
    # ✓ 会在 raw() 里抛 UnicodeEncodeError。
    # newline="\n":TextIOWrapper 默认会把 "\n" 翻成 "\r\n",而协议写的是
    # 「一行一个 JSON」,行尾就得真是 \n。stdin 那边翻译关掉也无妨 ——
    # serve_stdin 每行都 strip()。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass

    cfg = config_mod.load(args.config)
    path = config_mod.resolve(args.config)
    problems = rules_mod.validate(cfg.rules)
    if problems:
        for x in problems:
            print(f"[dot] 配置有问题:{x}", file=sys.stderr)
        return 2
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    if cfg.skills.enabled:
        skills_mod.ensure_dir(cfg)

    conn = db.connect(cfg.db)
    db.init(conn)
    conn.close()

    # config_path 必须传。不传的话 run_cli 里那段「给每条命令钉上 -c」
    # 等于关掉了 —— 界面点「列目标」可能读到另一份 dot.toml。
    bridge = Bridge(cfg, config_path=path)
    # 对话线程懒启动:第一次收到 chat 命令才建图(见 handle)。
    # 建图要几秒,不聊天就不该付这个成本。
    bridge.start_commands()
    if not args.no_loop:
        # 默认就开跑:这个产品的定位是常驻主动式,开着窗口就该在干活。
        # 不想让它跑就点顶栏的停止。
        bridge.start_loop()

    # ready 在起完循环之后才发:界面顶栏要画「● 值守中 60s」,「跑一轮」
    # 按钮要按循环状态置灰,而协议里没有「只查询」的入口。
    # (失败的路径下 fatal 可能先于 ready 到,界面按流处理即可。)
    bridge.out.event(
        "ready",
        version=__version__,
        config=str(path),
        workspace=str(cfg.workspace),
        db=str(cfg.db),
        bots=sorted(cfg.bots),
        loop=bridge.loop_running(),
        tick_seconds=cfg.tick_seconds,
    )

    try:
        return bridge.serve_stdin()
    except KeyboardInterrupt:
        bridge.shutdown()
        return 0
