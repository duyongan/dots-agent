# dots 桌面界面 · 计划一：Python 后端（bridge）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `python -m dots bridge` 成为一个可以用 JSON 行驱动的常驻后端：界面上要的四件事（看状态、下目标、聊天、控制循环）外加「所有 CLI 命令都能被执行」全部打通，并且能单独用手敲验证。

**Architecture:** 一个常驻进程，stdin/stdout 上一行一个 JSON。四条线程各管一摊：reader（主线程，独占读 stdin）、tick（`worker.tick()` 循环）、chat（`worker.chat_send()`）、command（跑 `cli.main(argv)`）。各线程持有自己的 `RunCtx` 和 sqlite 连接 —— 为此要把 `graph.CURRENT` 从模块全局改成线程局部。业务逻辑一行不重写，全部转手给 `worker` / `db` / `cli`。

**Tech Stack:** Python 3.11+，标准库（`threading` / `queue` / `json` / `contextlib`），复用现有的 langgraph + deepagents（不新增依赖）。

**依据：** `docs/superpowers/specs/2026-10-16-electron-desktop-gui-design.md`

---

## 提交说明（重要）

用户已明确要求：**本分支暂不提交**。所以下面每个任务末尾的「提交」步骤**先跳过**，只做代码与测试。等用户发话再统一提交。

---

## 为什么是这个设计（给零上下文的执行者）

三个数字决定了整个架构，先记住：

1. `from dots import cli` 在干净环境 **2.2 秒**，在开发机（conda base 装了 `transformers`，langchain 的 `language_models/base.py` 会 `try: from transformers import GPT2TokenizerFast`，顺手把 `torch` 拖进来）要 **8.7 秒**。
   → 所以后端必须**常驻**，界面点一下按钮不能现起一个进程。
2. `dots selftest` 要跑 30 秒。
   → 所以跑 CLI 命令要单独一条线程，不能堵住读 stdin 的那条。
3. 聊天里遇到 `send_email` 这类 ask 动作要弹确认框问你。
   → 所以**读 stdin 的线程必须永远是空闲的**，聊天得跑在别的线程上。这是四条线程设计里唯一真正不能省的一条理由。

## 文件结构

| 文件 | 动作 | 责任 |
|---|---|---|
| `dots/graph.py` | 改 | `CURRENT` 模块全局 → 线程局部；新增 `current()` / `set_current()` / `use_or_create()` |
| `dots/worker.py` | 改 | 8 处调用点改成新访问器；`_local_midnight` 挪走 |
| `dots/db.py` | 改 | 接收 `local_midnight()`（纯日期函数，放这儿才不连累 `dots cost` 的启动速度） |
| `dots/cli.py` | 改 | 重依赖改惰性导入；加 `bridge` 子命令 |
| `dots/bridge.py` | **新建** | 协议胶水：`Out` / `snapshot` / `Bridge` / `main` |
| `tests/test_bridge.py` | **新建** | bridge 的全部测试 |
| `tests/test_loop.py` | 改 | 8 处调用点改成新访问器 |

---

## Task 1: `graph.CURRENT` 改成线程局部

**为什么：** 现在它是模块级单值，`graph.py:55` 的注释自己标了 `ponytail: 全局单值;哪天要并发跑多个 goal,改成从 config 里取`。bridge 里 tick 和聊天并行跑，两边各有各的 `goal_id` / `bot` / `chat`，模块全局会被互相踩。

**Files:**
- Modify: `dots/graph.py:55-62`
- Modify: `dots/worker.py:47-48, 146-147, 284-289, 304-312, 326, 432-437`
- Modify: `tests/test_loop.py:105, 295-297, 338, 347, 369-370`
- Test: `tests/test_bridge.py`（本任务先建这个文件，只放一个用例）

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_bridge.py`：

```python
"""bridge 的测试。

分三层:
  纯函数级 —— handle() 的分发逻辑,不用起进程
  线程级   —— tick 循环 / 聊天的 ask 往返 / 退出复位
  进程级   —— 真起一个子进程喂 JSON 行(最后一节)

    python -m pytest tests/test_bridge.py -q
    python -m tests.test_bridge
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from dots import db, graph


def test_current_is_thread_local() -> str:
    """tick 和聊天跑在两条线程上,各自的 RunCtx 不能互相踩。

    这是 bridge 能成立的前提:模块全局的话,聊天线程设 chat=True,
    tick 线程就会以为自己在聊天,然后不调 complete_goal。
    """
    graph.set_current(graph.RunCtx(cfg=None, conn=None, bot="main"))
    seen: dict = {}

    def other() -> None:
        seen["before"] = graph.current()          # 子线程不该看见主线程的
        graph.set_current(graph.RunCtx(cfg=None, conn=None, bot="other"))
        time.sleep(0.05)                          # 让主线程有机会来踩
        seen["after"] = graph.current().bot

    t = threading.Thread(target=other)
    t.start()
    t.join()

    assert seen["before"] is None, "子线程看到了主线程的 RunCtx"
    assert seen["after"] == "other", seen
    assert graph.current().bot == "main", "子线程把主线程的 RunCtx 踩了"
    graph.set_current(None)
    return "  RunCtx 是线程局部的,两条线程互不干扰  ✓"
```

- [ ] **Step 2: 跑测试,确认它失败**

Run: `python -m pytest tests/test_bridge.py -q`
Expected: FAIL —— `AttributeError: module 'dots.graph' has no attribute 'set_current'`

- [ ] **Step 3: 改 `dots/graph.py`**

把 `dots/graph.py:55` 那一行 `CURRENT: RunCtx | None = None` 换成：

```python
# 线程局部,不是模块全局。桌面界面的 bridge 里 tick 和聊天跑在两条线程上,
# 各有各的 RunCtx(goal_id / bot / chat 都不同),模块全局会被两边互相踩。
#
# 别用模块级 __getattr__ 来糊:读会走 __getattr__,但 `graph.CURRENT = x`
# 是直接设模块属性 —— 设完之后读就再也走不到 __getattr__ 了,
# 线程局部语义会静默失效。所以老老实实给两个访问函数。
_local = threading.local()


def current() -> RunCtx | None:
    """当前线程的 RunCtx。这个线程没设过就返回 None。"""
    return getattr(_local, "ctx", None)


def set_current(ctx: RunCtx | None) -> None:
    _local.ctx = ctx


def use_or_create(cfg, conn) -> RunCtx:
    """本线程没有就建一个,有就把连接重新绑上。

    worker 里原来那几处 `if CURRENT is None: CURRENT = RunCtx(...)` 全走它。
    """
    ctx = current()
    if ctx is None:
        ctx = RunCtx(cfg=cfg, conn=conn)
        set_current(ctx)
    else:
        # 只「有就用现成的」不够。同一条长驻线程上会换连接:命令线程跑
        # `dots tick`,cli.main 每次自己开一条连接、跑完就关;第二次点「跑一轮」
        # 时 ctx 还攥着上一条已经关掉的连接,工具一写库就
        # `Cannot operate on a closed database` —— 而且会被 langgraph 吞成
        # 工具错误喂回模型,表现成「目标莫名其妙永远完不成」,极难查。
        ctx.cfg, ctx.conn = cfg, conn
    return ctx
```

在文件顶部 `import os` 那一组里加上 `import threading`。

再把 `_gate()` 改成**显式收 ctx**，不读线程局部：

```python
def _gate(ctx: RunCtx, action: str, args: dict) -> str | None:
    """放行返回 None;拦住返回一句给模型看的话。

    ctx 从工具闭包传进来,不读线程局部 —— 工具闭包捏的是 build 时的 ctx,
    这里再读一次线程局部就有了两个真相来源。今天每条线程都是「先 set_current
    再 build」所以两者一致;哪天变成 A 线程建图、B 线程 invoke,
    `rules_mod.decide(action, ctx.cfg.rules)` 就会用另一条线程的配置做审批判定
    (可能放行本该 ASK/DENY 的动作),audit 也会记到错的 goal_id 上。
    """
```

三处调用点在 `save_skill` / `send_email` / `run_shell` 里，都已有 `ctx` 在作用域内，改成 `_gate(ctx, "send_email", args)` 这种形式。

- [ ] **Step 4: 改 `dots/worker.py` 的 8 处调用点**

`tick()`（原 47-48 行）：

```python
    # tick 可以脱离 serve 单独跑(测试和 `dots tick` 都走这条路)
    graph.use_or_create(cfg, conn)
```

`_run()`（原 146-147 行）：

```python
    ctx = graph.current()
    ctx.goal_id = gid
    ctx.bot = bot
```

`chat_agents()`（原 284-289 行）：

```python
    ctx = graph.use_or_create(cfg, conn)
    if cfg.skills.enabled:
        skills_mod.ensure_dir(cfg)
    saver = checkpointer(cfg)
    return {"dot": graph.build(cfg, saver, ctx, bot="dot", chat=True)}, saver
```

`chat_send()`（原 304-312 行、326 行）：

```python
    ctx = graph.use_or_create(cfg, conn)
    agent = _pick(agents, "dot")
    config = {"configurable": {"thread_id": CHAT_THREAD}, "recursion_limit": 80}
    before = _messages(agent.get_state(config))

    ctx.goal_id = None
    ctx.bot = "dot"
    ctx.chat = True
```

`finally` 里那句 `graph.CURRENT.chat = False` 改成 `ctx.chat = False`。

`build_agents()`（原 432-437 行）：

```python
    ctx = graph.use_or_create(cfg, conn)
    if cfg.skills.enabled:
        skills_mod.ensure_dir(cfg)
    saver = checkpointer(cfg)
    return {name: graph.build(cfg, saver, ctx, bot=name) for name in cfg.bots}
```

- [ ] **Step 5: 改 `tests/test_loop.py` 的 8 处**

机械替换，逐个改：

`_setup()` 第 105 行：

```python
    graph.set_current(graph.RunCtx(cfg=cfg, conn=conn))
```

`_case_save_skill_validated()` 第 295-297 行：

```python
    graph.current().goal_id = "x"
    graph.current().bot = "dot"
    tools = {t.name: t for t in graph.make_tools(graph.current(), cfg.bot("dot"))}
```

第 338 行：

```python
    tools = {t.name: t for t in graph.make_tools(graph.current(), cfg.bot("dot"))}
```

第 347 行：

```python
            graph.current().goal_id = gid
```

第 369-370 行：

```python
    graph.current().goal_id = gid2
    graph.current().bot = "dot"
```

- [ ] **Step 6: 跑全套测试**

Run: `python -m pytest tests/ -q`
Expected: 26 passed（原来 24 + 新增 2）。特别注意 `test_loop` 里那几条交接和技能的用例仍然全绿 —— 它们直接读 `graph.current()`。

> 注：本任务最初只写了 1 个用例。代码质量审查指出它**没有真正制造并发**
> （`time.sleep(0.05)` 期间主线程停在 `t.join()` 上，没有任何写入），
> 而且 `use_or_create` 零覆盖 —— 而 worker 里 5 处改的都是它。
> 现在是两个用例：第一个用 `threading.Event` 把「两边都真写、对方写完再读」
> 的顺序钉死；第二个覆盖 `use_or_create` 的同线程复用、跨线程各建各的、
> 以及**换连接时重新绑定**。具体代码见质量审查报告。

- [ ] **Step 7: 提交（暂缓，见文首「提交说明」）**

```bash
git add dots/graph.py dots/worker.py tests/test_loop.py tests/test_bridge.py
git commit -m "RunCtx 改成线程局部,为桌面界面的并发做准备"
```

---

## Task 2: 让 `dots ls` 这类命令不再导入 langchain

**为什么：** `cli.py` 顶部 `from dots import worker`（→ `graph` → `deepagents` → `langchain` → 顺手 `torch`）。结果 `dots ls`（只是读 SQLite 里几行）也要等 8.7 秒。这不是为了界面才做的优化 —— 命令行下敲 `dots ls` 等 8 秒本来就是浪费。

**Files:**
- Modify: `dots/db.py`（加 `local_midnight`）
- Modify: `dots/worker.py:388, 399`（改用它）
- Modify: `dots/cli.py:17-24, 140-148, 249-265, 278-304, 442-477, 585-603`
- Modify: `tests/test_cli.py:176-192`（**计划原稿漏了这个，见 Step 4b**）
- Test: `tests/test_bridge.py`

> **原稿的漏点：** `tests/test_cli.py` 那两处用例（`test_plain_text_goes_to_the_agent_not_the_parser` 和它所在的 `run()`）用 `cli.worker.chat_agents` 打桩。`cli.py` 顶层的 `from dots import worker` 一删，`cli.worker` 这个模块属性就不存在了 —— `AttributeError: module 'dots.cli' has no attribute 'worker'`，全套会掉到 25 passed / 2 failed。
>
> 修法是**改测试**：把补丁打到真正的 `dots.worker` 模块对象上（惰性导入拿到的就是同一个模块对象，补丁照样生效）。**不要**给 `cli.py` 加模块级 `__getattr__` 去转发 `worker` —— 在生产代码里塞魔法只为迁就测试，是反的。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_bridge.py`：

```python
def test_light_commands_do_not_import_langchain() -> str:
    """`dots ls` / `log` / `cost` 不该付 langchain 的导入成本。

    开发机上这个链路是 8.7 秒(langchain 的 language_models/base.py 会
    `try: from transformers import GPT2TokenizerFast`,把 torch 一起拖进来)。
    列几行目标等 8 秒是不能接受的。

    用子进程跑,因为当前进程里 pytest 早就把 langchain 导进来了。
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        "from dots import cli\n"
        "heavy = sorted({m.split('.')[0] for m in sys.modules}\n"
        "               & {'deepagents', 'langchain', 'langchain_core', 'langgraph',\n"
        "                  'torch', 'transformers'})\n"
        "assert not heavy, heavy\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(root),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"import dots.cli 时拖进来了:{proc.stderr[-800:]}"
    return "  dots.cli 不再连带导入 langchain / torch  ✓"
```

- [ ] **Step 2: 跑测试,确认它失败**

Run: `python -m pytest tests/test_bridge.py::test_light_commands_do_not_import_langchain -q`
Expected: FAIL，stderr 里报出 `['deepagents', 'langchain', ...]`

- [ ] **Step 3: `_local_midnight` 挪到 `db.py`**

在 `dots/db.py` 的 `tokens_since` 上方加：

```python
def local_midnight() -> float:
    """今天零点的时间戳。

    放这儿而不是 worker 里:`dots cost` 要用它,而 worker 会连累
    cli 把 langchain 整个导进来(8.7 秒)。这是个纯日期函数,跟 SQL 层放一起
    比跟编排层放一起更合适。
    """
    n = datetime.now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
```

`db.py` 顶部加 `from datetime import datetime`。

删掉 `dots/worker.py` 里的 `_local_midnight` 定义（原 399-401 行），把 388 行的 `used = db.tokens_since(conn, _local_midnight())` 改成：

```python
    used = db.tokens_since(conn, db.local_midnight())
```

- [ ] **Step 4: `dots/cli.py` 的重依赖改惰性导入**

删掉顶部这两行：

```python
from dots import graph as graph_mod
from dots import worker
```

然后在每个用到的地方就地导入（每一处都带上为什么）：

`_cmd_repl()` 里建对话的那句之前（`session = _new_chat(args)` 所在的函数不用改，改 `_new_chat`）：

```python
def _new_chat(args):
    """建对话用的一张图。建不出来返回 None —— 那样 / 命令还能接着用。"""
    from dots import worker          # 惰性:只有真聊天才需要 langchain

    cfg = config_mod.load(args.config)
```

`_new_chat` 里原来的 `cfg = config_mod.load(args.config)` 保留在导入之后。

`_ask_agent()`：

```python
def _ask_agent(session, line: str) -> None:
    """一句话发给 agent,把回答打出来。

    审批在这里当场问 —— 后台是靠邮件/`/approve`,对话里当场问更顺。
    """
    from dots import worker          # 惰性,同上

    cfg, agents, conn, _ = session
```

`_dispatch()` 里 tick / serve 两处：

```python
    if args.cmd == "tick":
        from dots import worker      # 惰性:要跑 agent,本来就慢
        worker.serve(cfg, conn, dry_run=args.dry_run, once=True)
        return 0
    if args.cmd == "serve":
        from dots import worker
        worker.serve(
            cfg, conn, dry_run=args.dry_run, once=args.once,
            web=args.web, web_port=args.web_port,
        )
        return 0
```

`_cmd_cost()` 第 586 行：

```python
    day = db.tokens_since(conn, db.local_midnight())
```

`_cmd_doctor()` 里建模型那一处：

```python
    try:
        from dots import graph as graph_mod   # 惰性:doctor 要真建一个模型,这条路径本来就慢
        graph_mod.build_model(cfg)
        check("模型", True, cfg.model.name)
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py::test_light_commands_do_not_import_langchain -q`
Expected: PASS

- [ ] **Step 6: 实测一下提速**

Run: `time python -m dots selftest --help` 然后 `time python -m dots ls`
Expected: `ls` 从 8 秒级降到 1 秒级（本机是 conda 环境，`dots doctor` / `tick` / `serve` 仍然慢 —— 它们本来就要 langchain，这是对的）

注意：这两条命令要在有 `dot.toml` 的目录跑，没有的话 `dots ls` 会报「没找到配置」，那也是对的（报错很快）。

- [ ] **Step 7: 跑全套测试**

Run: `python -m pytest tests/ -q`
Expected: 27 passed

- [ ] **Step 8: 提交（暂缓）**

```bash
git add dots/db.py dots/worker.py dots/cli.py tests/test_bridge.py tests/test_cli.py
git commit -m "cli 重依赖改惰性导入,本机 dots ls 从 19s 降到 0.3s"
```

（`tests/test_cli.py` 是执行时发现的漏点，别漏了它。）

---

## Task 3: `dots/bridge.py` 骨架 —— 线程安全的输出

**Files:**
- Create: `dots/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_bridge.py`：

```python
def test_out_writes_one_json_line() -> str:
    """一行一个 JSON,且行内不能有换行 —— 对面是按行读的。"""
    from dots import bridge

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
    from dots import bridge

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


def test_out_is_thread_safe() -> str:
    """三条线程同时写,行不能被撕成两半。"""
    from dots import bridge

    buf = io.StringIO()
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
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k out`
Expected: FAIL —— `ModuleNotFoundError: No module named 'dots.bridge'`

- [ ] **Step 3: 建 `dots/bridge.py`**

```python
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
import io
import json
import queue
import sys
import threading
import time
import traceback

from dots import __version__
from dots import approvals, db
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
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k out`
Expected: 3 passed

- [ ] **Step 5: 提交（暂缓）**

---

## Task 4: 状态快照与分发骨架

**Files:**
- Modify: `dots/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_bridge.py`。先加一个和 `test_loop.py` 同款的夹具（本文件自足，不跨测试文件 import）：

```python
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
    from dots import bridge

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


def test_handle_state_empty() -> str:
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        b.handle({"id": 1, "cmd": "state"})

        msg = _lines(out)[-1]
        assert msg["id"] == 1 and msg["ok"] is True, msg
        data = msg["data"]
        assert data["goals"] == [] and data["approvals"] == [], data
        assert data["audit"] == [], data
    return "  state 返回三张表的全量快照  ✓"


def test_handle_goal() -> str:
    """下目标:落库、origin 标成 gui、状态直接 ready(界面下的目标 = 人授权的)。"""
    import tempfile
    from dots import bridge

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
    import tempfile
    from dots import bridge

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
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k handle`
Expected: FAIL —— `AttributeError: module 'dots.bridge' has no attribute 'Bridge'`

- [ ] **Step 3: 实现 `snapshot()` 和 `Bridge.handle()`**

追加到 `dots/bridge.py`：

```python
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
        }
    finally:
        conn.close()


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
        # worker.build_agents 的签名是 (cfg, conn),这里统一收成 (conn)
        self._make_agents = make_agents or (lambda conn: worker.build_agents(cfg, conn))
        self._make_chat = make_chat or (lambda conn: worker.chat_agents(cfg, conn))

        self._quit = threading.Event()
        self._stop = threading.Event()        # 整个进程退出
        self._loop_stop = threading.Event()   # 只停 tick 循环
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
            elif cmd == "chat":
                # 懒启动:建对话图要几秒,不聊天就不付这个成本
                self.start_chat()
                self._chat_q.put(msg)          # 丢给 chat 线程,不能堵住 reader
            elif cmd == "answer":
                self._deliver_answer(msg)
            elif cmd == "cli":
                self._cmd_q.put(msg)           # 同上,selftest 要 30 秒
            elif cmd == "loop":
                self._do_loop(rid, msg)
            elif cmd == "quit":
                self.out.reply(rid, {"bye": True})
                self._quit.set()
            else:
                self.out.reply(rid, error=f"不认识这条命令:{cmd!r}")
        except Exception as e:
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
        # 先推快照再应答:界面拿到 gid 时会去列表里找这条目标,
        # 快照没出去就找不到。
        self.push_state()
        self.out.reply(rid, {"gid": gid})

    def push_state(self) -> None:
        self.out.event("state", **snapshot(self.cfg))
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k handle`
Expected: 3 passed

- [ ] **Step 5: 提交（暂缓）**

---

## Task 5: CLI 通道 —— 所有命令都能被执行

**这是「所有命令必须变成按钮」的服务端。**

**Files:**
- Modify: `dots/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

```python
def test_cli_channel_captures_output() -> str:
    """跑一条 CLI 命令,把 stdout 和退出码拿回来。

    不逐个实现命令,而是复用 cli 自己那份命令表 —— 以后 dots 加子命令,
    界面加个按钮就行,两边都不用改协议。
    """
    import tempfile
    from dots import bridge

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
    """
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        for argv in (["serve"], [], ["serve", "--web"]):
            code, text = b.run_cli(argv)
            assert code == 2, (argv, code, text)
            assert "不能" in text or "拒" in text, text
    return "  serve / 空 argv 被拒,不会挂死也不会起第二个 worker  ✓"


def test_cli_channel_reports_argparse_errors() -> str:
    """argparse 打错参数会 sys.exit(2),得当成普通结果回传,不能把进程带走。"""
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        code, text = b.run_cli(["log", "--nope"])
        assert code == 2, (code, text)
        assert "nope" in text or "usage" in text.lower(), text
    return "  argparse 报错被接住,退出码透传  ✓"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k cli_channel`
Expected: FAIL —— `AttributeError: 'Bridge' object has no attribute 'run_cli'`

- [ ] **Step 3: 实现 `run_cli()`**

追加到 `dots/bridge.py`：

```python
# 这些命令不能从界面跑:
#   serve —— 常驻循环由 bridge 自己管,再起一个就是两个 worker 抢同一个库
#   空 argv —— 会掉进交互终端,那个阻塞在 input() 上,在子进程里就是挂死
_REFUSED = {"serve"}


def _cli_argv_ok(argv: list) -> str | None:
    """能跑返回 None,不能跑返回一句给界面看的话。"""
    if not argv:
        return "不能跑:没给命令。会掉进交互终端,那个要真人在终端前面。"
    head = str(argv[0])
    if head in _REFUSED:
        return (
            f"不能跑 {head!r}:常驻循环由界面自己管(顶栏那个启动/停止)。"
            f"再起一个就是两个 worker 抢同一个库。"
        )
    return None
```

在 `Bridge` 类里加：

```python
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
        except Exception as e:
            code = 1
            traceback.print_exc(file=sink)
            buf.write(f"{type(e).__name__}: {e}\n")

        return int(code), buf.getvalue()
```

再在模块里加 `_EventSink`：

```python
class _EventSink(io.TextIOBase):
    """把 print 变成一行行 output 事件,同时留一份在 buf 里。

    要装得像真 stdout:print() 会调 write(),有些库会问 isatty()。
    """

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
            self._out.event("output", id=self._rid, chunk=self._pending, done=True)
            self._pending = ""
```

模块顶部 `import contextlib`。

`handle()` 里 `cli` 分支不变（已经丢进 `_cmd_q`）。

- [ ] **Step 4: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k cli_channel`
Expected: 3 passed

- [ ] **Step 5: 提交（暂缓）**

---

## Task 6: tick 线程 + 启停 + 退出复位

**Files:**
- Modify: `dots/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

先加一个假图（和 `test_loop.py` 的 `StubAgent` 同款，本文件自足）：

```python
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


def test_loop_start_and_stop() -> str:
    """启停 tick 循环,并且它在真跑 worker.tick。"""
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([{"messages": [_ai("处理完了", tokens=10)]}] * 5)
        b = bridge.Bridge(cfg, out=out, make_agents=lambda conn: agent)

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

        b.handle({"id": 2, "cmd": "loop", "action": "stop"})
        _wait_for(lambda: not b.loop_running(), timeout=5)
        assert b.loop_running() is False
        _wait_for(lambda: not (b._loop_thread and b._loop_thread.is_alive()), timeout=5)
    return "  循环能起、能发现文件事件、能停  ✓"


def test_quit_puts_running_goal_back_to_ready() -> str:
    """正常退出时 running 的目标放回 ready。

    recover_stale_runs 的阈值是 1800 秒。正常关个窗口不该让一个跑到一半的
    目标白等半小时。
    """
    import tempfile
    from dots import bridge

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
```

再在夹具区加两个小工具：

```python
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
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k "loop_start or quit_puts"`
Expected: FAIL —— `AttributeError: 'Bridge' object has no attribute 'loop_running'`

- [ ] **Step 3: 实现 tick 线程**

**顺带把 `tick` 的服务端闸门补上。** 计划原先把「循环在跑时不许手动 tick」交给界面置灰，但界面是靠不住的 —— 服务端也得拦。在 `run_cli` 里 `_cli_argv_ok` 那一关的后面加：

```python
        if self.loop_running() and str(argv[0]) == "tick":
            return 2, (
                "不能跑 tick:后台循环正在跑(顶栏那个停止)。"
                "再跑一轮就是两条线程同时抢同一个库。"
            )
```

在 `Bridge` 里加：

```python
    # --------------------------------------------------------- tick 循环

    def loop_running(self) -> bool:
        t = self._loop_thread
        return bool(t and t.is_alive())

    def start_loop(self) -> bool:
        """起 tick 循环。已经在跑就什么都不做。"""
        if self.loop_running():
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
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            graph.set_current(graph.RunCtx(cfg=cfg, conn=conn))
            try:
                agents = self._make_agents(conn)
            except BaseException as e:
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
                except Exception:
                    # 照抄 worker.serve:一轮坏了不能干掉整个后端
                    detail = traceback.format_exc()[-1000:]
                    db.audit(conn, None, "tick_error", detail)
                    self.out.event("log", level="error", text=detail)
                    stats = None
                if stats:
                    line = worker.summarize(stats)
                    if line:
                        self.out.event("log", level="info", text=line)
                if self._stop.is_set() or self._loop_stop.is_set():
                    break
                self.push_state()
                self._sleep(cfg.tick_seconds)
        finally:
            conn.close()
            self.out.event("log", level="info", text="后台循环已停")

    def _sleep(self, seconds: float) -> None:
        """可被打断的等待。固定 sleep 的话点了停止还要等一分钟才停。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.is_set() or self._loop_stop.is_set():
                return
            time.sleep(0.2)
```

再加 `_do_loop` 和 `shutdown`：

```python
    def _do_loop(self, rid, msg) -> None:
        action = msg.get("action")
        if action == "start":
            self.out.reply(rid, {"running": True, "started": self.start_loop()})
        elif action == "stop":
            self.stop_loop()
            self.out.reply(rid, {"running": False})
        else:
            self.out.reply(rid, error=f"loop 的 action 只能是 start 或 stop,收到 {action!r}")

    # ------------------------------------------------------------- 退出

    def shutdown(self) -> None:
        """优雅退出:先让线程停,再把 running 的目标放回 ready。

        顺序不能反 —— 反过来,正在跑的那一轮可能在复位之后又把状态写回去。
        join 有超时:模型调用卡住时不能让关窗口变成关不掉。
        """
        self._stop.set()
        self._loop_stop.set()
        for t in (self._loop_thread, self._chat_thread, self._cmd_thread):
            if t and t.is_alive():
                t.join(timeout=3.0)
        self._reset_running()

    def _reset_running(self) -> None:
        conn = db.connect(self.cfg.db)
        try:
            for g in db.list_goals(conn, status=db.RUNNING, limit=100):
                db.set_goal(conn, g["id"], status=db.READY)
                db.audit(conn, g["id"], "bridge_shutdown", "界面退出,目标放回队列")
        finally:
            conn.close()
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k "loop_start or quit_puts"`
Expected: 2 passed

- [ ] **Step 5: 提交（暂缓）**

---

## Task 7: chat 线程 + 审批往返

**这是四条线程里唯一真正不能省的一条：** 聊天跑在 chat 线程上，所以 reader 线程永远空闲，能在聊天中途把「批不批」送进来。

**Files:**
- Modify: `dots/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

```python
class _Interrupt:
    def __init__(self, value):
        self.value = value


def test_chat_returns_reply() -> str:
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([{"messages": [_ai("inbox 里有 3 个文件", tokens=42)]}])
        b = bridge.Bridge(cfg, out=out,
                          make_chat=lambda conn: (agent, _NoSaver()))

        b.start_chat()
        b.handle({"id": 1, "cmd": "chat", "text": "工作区里有什么?"})
        _wait_for(lambda: any(m.get("event") == "chat" for m in _lines(out)))

        events = [m for m in _lines(out) if m.get("event") == "chat"]
        assert events[-1]["text"] == "inbox 里有 3 个文件", events[-1]
        assert events[-1]["tokens"] == 42, events[-1]
        assert any(m.get("event") == "chat_start" for m in _lines(out)), "没推 chat_start"
        b.shutdown()
    return "  聊天:推 chat_start -> 跑 -> 推回答和 token 数  ✓"


class _NoSaver:
    class conn:                      # noqa: N801 —— 只为了 .close() 能调
        @staticmethod
        def close():
            pass


def test_chat_ask_roundtrip() -> str:
    """聊天里碰到 ask 类动作:推 ask 事件,阻塞等界面回话。

    这条是四条线程设计的全部理由 —— 聊天跑在 chat 线程上,所以读 stdin 的
    reader 线程还闲着,answer 才送得进来。反过来就会死锁。
    """
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([
            {"__interrupt__": (_Interrupt(
                {"action": "send_email", "args": {"to": "alice@example.com"}}),)},
            {"messages": [_ai("邮件已发")]},
        ])
        b = bridge.Bridge(cfg, out=out, make_chat=lambda conn: (agent, _NoSaver()))
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
        # 第二次 invoke 必须带 resume={"approved": True}
        assert agent.calls[1][1].resume == {"approved": True}, agent.calls[1][1]
        b.shutdown()
    return "  聊天里的审批:推 ask -> 界面回 answer -> 带 approved 续跑  ✓"


def test_chat_ask_timeout_denies() -> str:
    """没人回话就按拒绝处理。一个永远等不到回复的审批 = 一个永远卡住的目标。"""
    import tempfile
    from dots import bridge

    with tempfile.TemporaryDirectory() as tmp:
        cfg, b, out = _setup(Path(tmp))
        agent = StubAgent([
            {"__interrupt__": (_Interrupt({"action": "run_shell", "args": {}}),)},
            {"messages": [_ai("好吧,那我不跑了")]},
        ])
        b = bridge.Bridge(cfg, out=out, make_chat=lambda conn: (agent, _NoSaver()))
        b.ASK_TIMEOUT = 0.2 if hasattr(b, "ASK_TIMEOUT") else None
        b.start_chat()
        b.handle({"id": 1, "cmd": "chat", "text": "跑个命令"})

        _wait_for(lambda: any(m.get("event") == "chat" for m in _lines(out)), timeout=10)
        assert agent.calls[1][1].resume == {"approved": False}, agent.calls[1][1]
        b.shutdown()
    return "  审批等超时按拒绝处理,聊天继续往下走  ✓"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k chat`
Expected: FAIL —— `AttributeError: 'Bridge' object has no attribute 'start_chat'`

- [ ] **Step 3: 实现 chat 线程**

在 `Bridge` 里加：

```python
    # ------------------------------------------------------------- 聊天

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
        conn = db.connect(cfg.db)
        db.init(conn)
        saver = None
        try:
            self.out.event("log", level="info", text="正在准备对话(建图要几秒)…")
            graph.set_current(graph.RunCtx(cfg=cfg, conn=conn))
            try:
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
            conn.close()
            # checkpointer 自己攥着一条 sqlite 连接,不关的话 Windows 上
            # 文件一直被锁着(worker.py 里为这个踩过两次)
            if saver is not None:
                try:
                    saver.conn.close()
                except Exception:
                    pass

    def _do_chat(self, agents, conn, msg) -> None:
        text = str(msg.get("text") or "").strip()
        if not text:
            self.out.event("chat", text="(空消息,没发出去)")
            return
        self.out.event("chat_start", text=text)
        try:
            reply, used = worker.chat_send(agents, self.cfg, conn, text, self._ask)
        except Exception as e:
            detail = traceback.format_exc()[-600:]
            self.out.event("chat", text=f"{type(e).__name__}: {e}", error=True)
            self.out.event("log", level="error", text=detail)
            return
        self.out.event("chat", text=reply, tokens=used)
        self.push_state()

    # ---------------------------------------------------- 聊天里的审批

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

    def _deliver_answer(self, msg) -> None:
        qid = str(msg.get("qid") or "")
        with self._lock:
            self._answers[qid] = bool(msg.get("value"))
            ev = self._waiting.get(qid)
        if ev is not None:
            ev.set()
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k chat`
Expected: 3 passed

`test_chat_ask_timeout_denies` 里那行 `b.ASK_TIMEOUT = ...` 是错的（常量在模块级）。改成 monkeypatch 模块常量，把测试里那段换成：

```python
        import dots.bridge as bridge_mod

        old = bridge_mod.ASK_TIMEOUT
        bridge_mod.ASK_TIMEOUT = 0.2
        try:
            b.start_chat()
            b.handle({"id": 1, "cmd": "chat", "text": "跑个命令"})
            _wait_for(lambda: any(m.get("event") == "chat" for m in _lines(out)),
                      timeout=10)
        finally:
            bridge_mod.ASK_TIMEOUT = old
```

- [ ] **Step 5: 提交（暂缓）**

---

## Task 8: 命令线程 + `main()` 入口 + `dots bridge` 子命令

**Files:**
- Modify: `dots/bridge.py`
- Modify: `dots/cli.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: 写失败的测试**

```python
def test_cli_command_goes_through_the_command_thread() -> str:
    """命令必须异步跑:selftest 要 30 秒,不能让 reader 线程干等。"""
    import tempfile
    from dots import bridge

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
    import subprocess
    import sys
    import tempfile

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
            [sys.executable, "-m", "dots", "bridge"],
            cwd=str(cfgdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            env={**__import__("os").environ, "PYTHONPATH": str(root),
                 "PYTHONIOENCODING": "utf-8"},
        )
        try:
            # 第一行必须是 ready(也可能先是 fatal,环境不对时)
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
            # id 必须带(界面靠它把输出贴到对应命令下面),但 output 事件
            # 带着同一个 id,所以还得加 "ok" in m 才认得出是应答。
            c = read_until(lambda m: m.get("id") == 3 and "ok" in m)
            assert c["data"]["code"] == 0, c

            send({"id": 4, "cmd": "quit"})
            read_until(lambda m: m.get("id") == 4)
        finally:
            proc.stdin.close()
            proc.wait(timeout=15)
    return "  真起子进程:ready -> state -> goal -> cli -> quit 全通  ✓"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python -m pytest tests/test_bridge.py -q -k "command_thread or stdin_loop"`
Expected: FAIL —— `AttributeError: 'Bridge' object has no attribute 'start_commands'`

- [ ] **Step 3: 实现命令线程和 `main()`**

在 `Bridge` 里加：

```python
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
            code, text = self.run_cli(argv, rid=rid)
            self.out.reply(rid, {"code": code, "out": text})
            self.push_state()
```

模块级加 `main()`：

```python
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dots bridge", description="给桌面界面用的 JSON-lines 后端(一般不用手敲)"
    )
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--no-loop", action="store_true",
                        help="起来了先不跑 tick 循环")
    args = parser.parse_args(argv)

    # Windows 上 stdin/stdout 默认是 GBK,中文 JSON 出去就是乱码
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
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
    bridge.out.event(
        "ready",
        version=__version__,
        config=str(config_mod.resolve(args.config)),
        workspace=str(cfg.workspace),
        db=str(cfg.db),
        bots=sorted(cfg.bots),
    )
    # 对话线程懒启动:第一次收到 chat 命令才建图(见 handle)。
    # 建图要几秒,不聊天就不该付这个成本。
    bridge.start_commands()
    if not args.no_loop:
        # 默认就开跑:这个产品的定位是常驻主动式,开着窗口就该在干活。
        # 不想让它跑就点顶栏的停止。
        bridge.start_loop()

    try:
        return bridge.serve_stdin()
    except KeyboardInterrupt:
        bridge.shutdown()
        return 0
```

再在 `Bridge` 里加读 stdin 的主循环：

```python
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
```

- [ ] **Step 4: 给 `cli.py` 加 `bridge` 子命令**

在 `_parser()` 里 `selftest` 那一行旁边加：

```python
    # 子解析器必须自己声明 --no-loop / -c,否则 `dots bridge --no-loop`
    # 会被顶层 argparse 判成 unrecognized arguments。
    # default=SUPPRESS 是必须的:subparser 的默认值会覆盖顶层命名空间,
    # 不压住的话 `dots -c x bridge` 里前面那个 -c 会被抹成 None。
    br = sub.add_parser("bridge", help="给桌面界面用的 JSON-lines 后端(一般不用手敲)")
    br.add_argument("-c", "--config", default=argparse.SUPPRESS)
    br.add_argument("--no-loop", action="store_true", default=argparse.SUPPRESS)
```

在 `main()` 里，**放在 `config_mod.load()` 之前**（bridge 自己读配置）：

```python
    if args.cmd == "bridge":
        from dots import bridge as bridge_mod   # 惰性:只有界面用得上
        argv = []
        if getattr(args, "config", None):
            argv += ["-c", args.config]
        if getattr(args, "no_loop", False):
            argv += ["--no-loop"]
        return bridge_mod.main(argv)
```

再补一条闸门：交互终端里敲 `/bridge` 会把 stdin 抢走(bridge 自己按行读,
`_cmd_repl` 的 `input()` 就再也拿不到输入)。在 `_cmd_repl` 的命令分发那里加:

```python
            if argv[0] == "bridge":
                # bridge 要独占 stdin,在这儿跑会把 input() 饿死
                print("bridge 是给桌面界面用的,不能在交互终端里跑。", file=sys.stderr)
                continue
```

放在 `if args.cmd == "selftest":` 那几行旁边。

- [ ] **Step 5: 跑测试,确认通过**

Run: `python -m pytest tests/test_bridge.py -q -k "command_thread or stdin_loop"`
Expected: 2 passed

- [ ] **Step 6: 手工敲一遍（这条就是验收标准本身）**

```bash
cd /tmp && mkdir -p bridgetest && cd bridgetest
mkdir -p inbox
printf '[dot]\nworkspace = "workspace"\ndb = "dot.db"\n[model]\nname = "fake:model"\n' > dot.toml
printf 'a\nb\nc\n' > inbox/todo.txt
PYTHONPATH=/path/to/dots-agent python -m dots bridge --no-loop
```

然后一行行敲进去（每行回车一次）：

```json
{"id":1,"cmd":"state"}
{"id":2,"cmd":"goal","text":"把 inbox 整理一下"}
{"id":3,"cmd":"cli","argv":["ls"]}
{"id":4,"cmd":"cli","argv":["serve"]}
{"id":5,"cmd":"quit"}
```

Expected：每一行都有对应的 JSON 应答；`ls` 的输出在 `data.out` 里（中文正常不乱码）；`serve` 被拒；`quit` 之后进程退出、退出码 0。

- [ ] **Step 7: 提交（暂缓）**

---

## Task 9: 全套回归 + 接入 `dots selftest`

**Files:**
- Modify: `tests/test_bridge.py`
- Modify: `dots/cli.py`（`_cmd_selftest`）

- [ ] **Step 1: 加 `run()` 和 pytest 入口**

`test_bridge.py` 末尾：

```python
def run() -> str:
    cases = [
        test_current_is_thread_local,
        test_light_commands_do_not_import_langchain,
        test_out_writes_one_json_line,
        test_out_reply_shape,
        test_out_is_thread_safe,
        test_handle_state_empty,
        test_handle_goal,
        test_handle_bad_input,
        test_cli_channel_captures_output,
        test_cli_channel_refuses_blocking_commands,
        test_cli_channel_reports_argparse_errors,
        test_loop_start_and_stop,
        test_quit_puts_running_goal_back_to_ready,
        test_chat_returns_reply,
        test_chat_ask_roundtrip,
        test_chat_ask_timeout_denies,
        test_cli_command_goes_through_the_command_thread,
        test_stdin_loop_end_to_end,
    ]
    notes = []
    for case in cases:
        notes.append(case())
    return "\n".join(notes)


def test_all_bridge():
    run()


if __name__ == "__main__":
    print(run())
```

同时把每个测试函数上多余的 `import tempfile / from dots import bridge` 移到文件顶部（写的时候图快就地导入了，收尾统一）。顶部加上：

```python
import os
import subprocess
import sys
import tempfile
```

- [ ] **Step 2: 把它接进 `dots selftest`**

`dots/cli.py` 的 `_cmd_selftest()` 里，`from tests import ...` 那一行加上 `test_bridge`：

```python
    try:
        from tests import test_bridge, test_cli, test_loop, test_sources, test_web
    except ImportError:
        ...

    print("== 事件源(本地假服务)==")
    print(test_sources.run())
    print("== 一轮 tick(用假模型)==")
    print(test_loop.run())
    print("== 桌面后端(bridge)==")
    print(test_bridge.run())
    print("== 活动视图 ==")
    print(test_web.run())
    print("== 交互终端 ==")
    print(test_cli.run())
```

- [ ] **Step 3: 跑全套**

Run: `python -m pytest tests/ -q`
Expected: 44 passed（原 24 + bridge 的 18 + Task 1/2 里那两个）

- [ ] **Step 4: 跑 selftest**

Run: `python -m dots selftest`
Expected: 末尾打印「自检通过。」，中间多出 `== 桌面后端(bridge) ==` 一节

- [ ] **Step 5: 提交（暂缓）**

---

## Task 10: 并发冒烟（需要真模型，手工）

**为什么单独一步：** 前面所有测试用的都是假图。**真问题是 langgraph / deepagents 的两张图能不能在同一进程的两条线程上同时跑。** 这件事没验证过就往上盖界面，风险太大。

**Files:** 无（只跑验证）

- [ ] **Step 1: 确认有可用的模型**

Run: `python -m dots doctor`
Expected: 「模型 ✓」。没有的话跳到 Step 4，把这条记成待验证。

- [ ] **Step 2: 起 bridge，同时开跑 tick 和聊天**

```bash
cd <一个有 dot.toml 的目录>
python -m dots bridge
```

敲进去（不关 tick 循环，默认就是开着的）：

```json
{"id":1,"cmd":"goal","text":"读 workspace/inbox 下的文件,写一份 50 字的摘要"}
{"id":2,"cmd":"chat","text":"你好,你是谁?"}
{"id":3,"cmd":"chat","text":"刚才那个目标现在什么状态?"}
{"id":4,"cmd":"state"}
```

Expected：
- tick 在跑目标的同时，`chat` 能正常回话（不卡、不串）
- `state` 里那条 goal 最终变成 `done` 或 `pending_review`
- 进程没有崩溃、没有 traceback

- [ ] **Step 3: 往 `inbox/` 扔个文件，聊天里问它看到了没**

验证两条线程读的是同一个工作区、同一份库。

- [ ] **Step 4: 把结果记进 spec 的「已知粗糙处」或者新开一条**

并发跑不通的话，退路是：chat 线程在 tick 跑目标时排队等待（`worker.tick` 外面加锁），代价是目标跑几分钟时聊天没反应。**先测，再决定要不要退。**

---

## 自检结果

**spec 覆盖检查：**

| spec 章节 | 落在哪个任务 |
|---|---|
| §3.1 `CURRENT` 线程局部 | Task 1 |
| §3.2 `bridge.py` 结构 | Task 3-8 |
| §3.3 cli 惰性导入 | Task 2 |
| §3.4 通用命令通道 | Task 5、8 |
| §4 协议（7 条命令 / 9 种事件） | Task 4（state/goal）、5（cli/output）、6（loop/quit）、7（chat/chat_start/ask）、8（ready/exit） |
| §6 错误处理：单轮 tick 不干掉后端 | Task 6 `_tick_loop` |
| §6 退出复位 running | Task 6 `shutdown` |
| §7 Python 测试 | 每个任务 |
| §8 `resolveBackend()` | **不在本计划** —— 属于计划二（Electron） |
| §10 v1 不做主题导入/自带运行时 | 本计划不涉及 |

**遗留：** `spec §4` 里的 `exit` 事件本计划没有实现（Node 侧靠 stdout 的 EOF 就能知道进程没了，多余的机制先不加）。计划二里如果发现需要，再补。

**类型一致性检查：** `Out.raw/reply/event`、`Bridge.__init__(cfg, *, out, config_path, make_agents, make_chat)`、`Bridge.handle/push_state/run_cli/start_loop/stop_loop/loop_running/start_chat/start_commands/shutdown/serve_stdin`、`graph.current/set_current/use_or_create`、`db.local_midnight` —— 全文用的都是这几个名字。

**自查时改掉的四处（照原样写会跑不通）：**

1. `make_agents` 默认值直接写 `worker.build_agents`，但它签名是 `(cfg, conn)`，调用处只传了 `(conn)` → 改成包一层 lambda
2. `_setup()` 没写 `dot.toml`，而 `run_cli` 会跑 `cli.main()` → 会去读开发机真实的 `~/.dots/dot.toml`，测试变成看运气 → 改成临时目录里写一份，并给 Bridge 加 `config_path` 钉住它
3. `main()` 里无条件 `start_chat()`，但那个线程一启动就建真图 → e2e 测试用假模型名会直接 fatal → 改成第一次收到 `chat` 命令才懒启动
4. `_wait_for(lambda: db.list_goals(conn=_open(cfg)))` 每次调用漏一条连接 → 改成一个正常关闭连接的小闭包

**已知粗糙处（v1 接受，记着）：**

1. **命令没有超时、也不能取消。** `_cmd_loop` 一条一条串行跑,`run_cli` 里没有超时 —— 一条卡住的命令(`selftest` 30 秒起步)会把命令通道整个堵死,界面所有按钮一起转圈。v1 接受(命令都是短任务),v1 之后要做可中断 + 超时。
2. **tick 线程往 stderr 打的诊断行会被算进正在跑的那条命令的 output。** `run_cli` 用的是 `contextlib.redirect_stdout/stderr`,那是**进程全局**的,而 worker 里那几处 `print(..., file=sys.stderr)`(`worker.py` 的 run 失败提示、`sources.py` 的源错误)不认线程。罕见(命令行跑的时候正好有目标失败),先接受。
