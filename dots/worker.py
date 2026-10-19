"""一个 tick 的编排。worker 是唯一编排者 —— CLI 和 sources 都不许直接调 graph。

一轮 tick 四件事,顺序不能换:
  1. 收事件落库(幂等靠 events 表的 UNIQUE)
  2. 路由:审批回信 → 处理;其它 → 建目标
  3. 已决定的审批 → 续跑那个目标
  4. 挑一个 ready 的目标 → 跑

审批的所有入口(邮件回复、CLI approve)最终都只是往 approvals 表写一个决定,
由第 3 步统一续跑。这样 CLI 和邮件共用一条路径,不需要 IPC。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from dots import approvals, db, graph, skills as skills_mod, sources


# ------------------------------------------------------------------- 一轮 tick

def _pick(agents, bot: str):
    """agents 可以是 {bot: 图},也可以是单个图(测试用)。

    单个图当成「谁问都给这个」—— 测试不想为每个 bot 准备一份桩。
    """
    if isinstance(agents, dict):
        return agents.get(bot) or agents.get("*") or next(iter(agents.values()))
    return agents


def tick(cfg, conn, agents, *, dry_run: bool = False) -> dict:
    stats = {
        "events_new": 0, "approval_replies": 0, "goals_new": 0,
        "resumed": 0, "ran": 0, "interrupted": 0, "failed": 0,
        "handoffs": 0, "skipped": 0, "paused": None,
    }
    # tick 可以脱离 serve 单独跑(测试和 `dots tick` 都走这条路)
    if graph.CURRENT is None:
        graph.CURRENT = graph.RunCtx(cfg=cfg, conn=conn)

    # 1. 收事件
    for e in sources.poll_all(cfg, conn):
        if db.add_event(conn, e.source, e.key, e.payload):
            stats["events_new"] += 1

    # 2. 路由
    for row in db.new_events(conn):
        payload = json.loads(row["payload"] or "{}")

        if row["source"] == "imap" and approvals.is_approval_subject(
            cfg.approval_subject_prefix, str(payload.get("subject", ""))
        ):
            _apply_reply(cfg, conn, payload, stats)
            db.consume_event(conn, row["id"], None, "approval")
            continue

        text = describe_event(row["source"], payload)
        if not text:
            db.consume_event(conn, row["id"], None, "skipped")
            continue
        # 外部事件直接 ready:你已经把它写进 dot.toml 了,那就是授权。
        # agent 自己派生出来的目标才需要人工批。
        gid = db.add_goal(conn, text, origin=f"event:{row['source']}", status=db.READY)
        db.consume_event(conn, row["id"], gid)
        db.audit(conn, gid, "goal_from_event", f"{row['source']}:{row['key']}")
        stats["goals_new"] += 1

    # 3. 先让超时的审批过期,再决定谁能续跑
    if cfg.approval_timeout_hours:
        cutoff = time.time() - cfg.approval_timeout_hours * 3600
        expired = db.expire_approvals(conn, cutoff)
        if expired:
            db.audit(
                conn, None, "approvals_expired",
                f"{expired} 个挂起动作超过 {cfg.approval_timeout_hours}h 没人批,按拒绝处理",
            )

    for goal in db.list_goals(conn, status=db.INTERRUPTED, limit=20):
        if db.pending_approvals(conn, goal["id"]):
            continue  # 还在等人
        row = db.last_decided_approval(conn, goal["id"])
        if row is None:
            continue
        resume = {"approved": row["status"] == approvals.APPROVED}
        db.audit(conn, goal["id"], "resume", f"{row['token']} -> {row['status']}")
        outcome = _run(
            cfg, conn, agents, goal, dry_run=dry_run, stats=stats,
            resume=resume, from_status=db.INTERRUPTED,
        )
        if outcome != "skipped":
            stats["resumed"] += 1

    # 4. 跑一个
    if _paused(cfg, conn, stats):
        return stats
    goal = db.pick_ready(conn)
    if goal is not None:
        _run(cfg, conn, agents, goal, dry_run=dry_run, stats=stats)

    return stats


def describe_event(source: str, payload: dict) -> str:
    if source == "fs":
        return (
            f"工作区里出现了新文件 {payload.get('path')}({payload.get('size')} 字节)。\n"
            f"读它,按你自己的判断处理。有产出就写进工作区,"
            f"然后用 complete_goal 说清楚你做了什么、证据在哪。"
            f"如果它不值得处理,也用 complete_goal 说明理由。"
        )
    if source == "ical":
        return (
            f"日程提醒:{payload.get('summary')} 将在 {payload.get('minutes_away')} 分钟后开始"
            f"({payload.get('starts_at')})。提前做点准备,把该准备的东西写进工作区。"
        )
    if source == "imap":
        return (
            f"收到一封邮件。\n"
            f"发件人:{payload.get('from')}\n"
            f"主题:{payload.get('subject')}\n\n"
            f"{str(payload.get('body', ''))[:2000]}"
        )
    return ""


# --------------------------------------------------------------------- 跑一次

def _run(cfg, conn, agents, goal, *, dry_run: bool, stats: dict, resume=None,
         from_status: str = db.READY) -> str:
    gid = goal["id"]
    # 原子的:抢不到说明另一个进程已经在跑它了,直接让开
    if not db.claim_goal(conn, gid, from_status):
        return "skipped"

    bot = goal["bot"]
    agent = _pick(agents, bot)
    graph.CURRENT.goal_id = gid
    graph.CURRENT.bot = bot
    config = {"configurable": {"thread_id": gid}, "recursion_limit": 80}

    before = _messages(agent.get_state(config))
    before_files = {k for k, _, _, _ in sources.fs_scan(cfg)}
    try:
        if resume is not None:
            result = agent.invoke(Command(resume=resume), config)
        elif agent.get_state(config).next:
            # 图停在半路(上次崩在这,或者审批刚批完但 resume 值丢了):接着走
            result = agent.invoke(None, config)
        else:
            text = _kickoff_text(goal)
            db.audit(conn, gid, "run_start", f"[{bot}] {text[:200]}")
            result = agent.invoke({"messages": [HumanMessage(text)]}, config)
    except Exception as e:
        db.set_goal(conn, gid, status=db.FAILED)
        db.audit(conn, gid, "run_failed", f"{type(e).__name__}: {e}")
        print(f"[dot] 目标 {gid} 跑失败:{type(e).__name__}: {e}", file=sys.stderr)
        stats["failed"] += 1
        return "failed"
    finally:
        # 必须放在 finally:失败的 run 也会在工作区留下文件,
        # 不登记的话下一轮就会拿 agent 的半成品当新输入,一轮变两轮。
        _suppress_self_writes(cfg, conn, gid, before_files)

    used = _tokens_new(before, _messages(result))
    db.add_tokens(conn, gid, used)
    db.audit(conn, gid, "run_done", "", tokens=used)

    intr = (result or {}).get("__interrupt__")
    if intr:
        payload = getattr(intr[0], "value", None) or {}
        db.set_goal(conn, gid, status=db.INTERRUPTED)
        approvals.send(cfg, conn, gid, payload, dry_run=dry_run)
        stats["interrupted"] += 1
        return "interrupted"

    after = db.get_goal(conn, gid)
    if after["bot"] != bot:
        # 交接发生了。这一轮 agent 之后还做了什么一律不算 —— 目标已经换人,
        # 交给下一个 bot 从头接手。handoff_to 自己已经把状态置成 READY,
        # 这里只是把「交接后还调了 complete_goal」这种情况盖回去。
        if after["status"] != db.READY:
            db.set_goal(conn, gid, status=db.READY)
        db.audit(conn, gid, "handoff_end", f"{bot} 交出去了,等 {after['bot']} 接手")
        stats["handoffs"] += 1
        return "handoff"

    # 工具里调过 complete_goal 的话状态已经被改了;还是 running 就是没说完成。
    if after["status"] == db.RUNNING:
        db.set_goal(
            conn, gid, status=db.REVIEW,
            summary="这一轮结束了,但没有调用 complete_goal",
        )
        db.audit(conn, gid, "no_completion", "跑完了但没说完成")
    stats["ran"] += 1
    return "ran"


def _kickoff_text(goal) -> str:
    """交接过来的目标要带上前任的简报,不然新 bot 是懵的。"""
    if goal["handoff_from"]:
        return (
            f"【{goal['handoff_from']} 把这件事交给了你】\n\n"
            f"{goal['handoff_note'] or '(前任没留下简报)'}\n\n"
            f"原始目标:{goal['text']}"
        )
    return goal["text"]


def _suppress_self_writes(cfg, conn, gid: str, before_keys: set[str]) -> int:
    """agent 往工作区写出来的东西,会被自己的 fs 源当成下一轮的新事件。

    后果是自激循环:处理 todo.txt -> 写出 todo_review.md -> 被当成新文件 ->
    再处理一遍 -> 又写一遍。跑一晚上能空转掉一整天的额度。

    做法:把这一轮「新出现」的文件 key 以 status='self' 预先插进 events 表,
    下一轮 fs_poll 报出同样的 key 时被 UNIQUE 挡掉。

    只压这一轮新增的 key,所以你在我跑的时候往 inbox 扔的文件不受影响。
    """
    suppressed = 0
    for key, relpath, _, _ in sources.fs_scan(cfg):
        if key in before_keys:
            continue
        if db.add_event(conn, "fs", key, {"path": relpath, "by": "agent"}, status="self"):
            suppressed += 1
    if suppressed:
        db.audit(conn, gid, "suppressed_self_writes", f"压掉 {suppressed} 个 agent 自产文件")
    return suppressed


def _messages(state) -> list:
    """吃两种东西:graph 的 StateSnapshot,和 invoke 返回的普通 dict。

    注意 dict 也有 .values —— 那是内置方法,不是数据。必须先判 dict。
    """
    if state is None:
        return []
    if isinstance(state, dict):
        return list(state.get("messages") or [])
    values = getattr(state, "values", None) or {}
    return list(values.get("messages", []) or [])


def _tokens_new(before: list, after: list) -> int:
    """只算这一轮新产生的消息。

    ponytail: 按消息 id 去重,续跑时不会把历史重复计入。
    没有 id 的消息会各算一次 —— 是上界,不是精确值。
    """
    seen = {m.id for m in before if getattr(m, "id", None) is not None}
    total = 0
    for m in after:
        mid = getattr(m, "id", None)
        if mid is not None and mid in seen:
            continue
        usage = getattr(m, "usage_metadata", None)
        if isinstance(usage, dict):
            total += int(usage.get("input_tokens") or 0)
            total += int(usage.get("output_tokens") or 0)
    return total


# ------------------------------------------------------------------- 对话

CHAT_THREAD = "chat"

# tick 和聊天共用的闸门。两者都会动 graph.CURRENT 和 checkpointer,
# 单进程里必须串行 —— tick 是后台线程,聊天是 HTTP 线程。
RUN_LOCK = threading.RLock()


@contextmanager
def use_ctx(ctx):
    """临时把全局 RunCtx 换成这次的。

    graph 的工具闭包各个不一样,但 _gate() 读的是 graph.CURRENT;
    不换的话,后台 tick 的规则和连接会被聊天借走。
    """
    prev = graph.CURRENT
    graph.CURRENT = ctx
    try:
        yield ctx
    finally:
        graph.CURRENT = prev


def chat_agents(cfg, conn):
    """对话用的那张图,返回 (agents, saver, ctx)。

    单独 build 一次是为了带上 CHAT_PROMPT。saver 要还给调用方 ——
    它自己攥着一条 sqlite 连接,不关的话在 Windows 上会一直锁着 db 文件,
    临时目录删不掉(踩过两次)。

    ctx 也单独建:GUI 里后台 tick 和聊天各用各的连接和规则,
    不能借用 graph.CURRENT 里那个。
    """
    if cfg.skills.enabled:
        skills_mod.ensure_dir(cfg)
    ctx = graph.RunCtx(cfg=cfg, conn=conn)
    saver = checkpointer(cfg)
    return {"dot": graph.build(cfg, saver, ctx, bot="dot", chat=True)}, saver, ctx


def chat_config() -> dict:
    return {"configurable": {"thread_id": CHAT_THREAD}, "recursion_limit": 80}


def chat_turn(agent, ctx, payload) -> tuple[dict, dict | None]:
    """对对话图做一次 invoke。返回 (state, 中断载荷或 None)。

    payload 可以是 {"messages": [HumanMessage(...)]},也可以是
    Command(resume=...)。调用方负责拿 RUN_LOCK 和 use_ctx ——
    GUI 一次 HTTP 请求只走到「中断」或「回答」为止,审批在下一个请求里续。
    """
    ctx.goal_id = None
    ctx.bot = "dot"
    ctx.chat = True
    try:
        result = agent.invoke(payload, chat_config()) or {}
    finally:
        # 用完就复位。tick 复用同一个上下文字段,带着 chat=True 跑后台目标,
        # agent 会以为自己在聊天,不调 complete_goal。
        ctx.chat = False
    intr = result.get("__interrupt__")
    if not intr:
        return result, None
    return result, (getattr(intr[0], "value", None) or {})


def chat_send(agents, cfg, conn, text: str, decide) -> tuple[str, int]:
    """把一句话发给 agent,拿回 (回答, 花了多少 token)。

    和 _run 的区别是**没有 goal 行**:对话不是目标,不进队列,后台 tick 不碰它。
    靠固定的 thread_id 记住上下文 —— 换了进程也还在,checkpointer 里存着。

    代价说清楚:所以 complete_goal 在这儿是空转、handoff 会被拦(见 make_tools),
    `/ls` 里也看不到。要交给后台排队跑,用 `/goal`。

    decide(payload) -> bool 是审批回调:ask 类动作怎么问你,由调用方决定
    (终端里当场问)。
    """
    if graph.CURRENT is None:
        graph.CURRENT = graph.RunCtx(cfg=cfg, conn=conn)
    ctx = graph.CURRENT
    agent = _pick(agents, "dot")
    before = _messages(agent.get_state(chat_config()))

    payload = {"messages": [HumanMessage(text)]}
    with use_ctx(ctx):
        for _ in range(20):            # 一个人能连着批 20 次还不收手,那是脚本
            result, intr = chat_turn(agent, ctx, payload)
            if intr is None:
                break
            payload = Command(resume={"approved": bool(decide(intr))})

    # 用最后一次 invoke 的返回值,不再 get_state 一趟 —— 同一份数据,少一次往返。
    after = _messages(result)
    used = _tokens_new(before, after)
    db.audit(conn, None, "chat", text[:200], tokens=used)
    return _last_reply(after), used


def _last_reply(messages: list) -> str:
    """最后一条有内容的 AI 消息。工具调用那种空消息跳过。"""
    for m in reversed(messages):
        if getattr(m, "type", "") not in ("ai", "assistant"):
            continue
        text = _flatten(getattr(m, "content", ""))
        if text.strip():
            return text.strip()
    return "(它没说话)"


def _flatten(content) -> str:
    """content 可能是字符串,也可能是 [{'type':'text','text':...}, ...]。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content or "")


# --------------------------------------------------------------------- 审批

def _apply_reply(cfg, conn, payload: dict, stats: dict) -> None:
    parsed = approvals.parse_reply(
        cfg, str(payload.get("subject", "")), str(payload.get("body", ""))
    )
    if parsed is None:
        db.audit(
            conn, None, "approval_unparsed",
            f"主题认得出来,正文没看懂:{str(payload.get('subject', ''))[:100]}",
        )
        return
    token, decision = parsed
    if not db.resolve_approval(conn, token, decision):
        db.audit(conn, None, "approval_duplicate", f"{token} 已经批过了")
        return
    row = db.get_approval(conn, token)
    db.audit(conn, row["goal_id"], "approval_reply", f"{token} -> {decision}")
    stats["approval_replies"] += 1


# --------------------------------------------------------------------- 预算

def _paused(cfg, conn, stats: dict) -> bool:
    """budget=0 = 不限制。填了数就在这里拦 —— 不动 agent 一行代码。"""
    if not cfg.daily_token_budget:
        return False
    used = db.tokens_since(conn, _local_midnight())
    if used < cfg.daily_token_budget:
        return False
    stats["paused"] = f"今日已用 {used}/{cfg.daily_token_budget} token,暂停跑新目标"
    key = f"budget_paused:{datetime.now().date().isoformat()}"
    if db.meta_get(conn, key) is None:   # 一天只报一次,别刷屏
        db.meta_set(conn, key, "1")
        db.audit(conn, None, "budget_paused", stats["paused"])
    return True


def _local_midnight() -> float:
    n = datetime.now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# --------------------------------------------------------------------- 主循环

def checkpointer(cfg):
    """checkpointer 必须用自己的一条连接。

    不能和应用库共用连接:两边都调 conn.commit()。我这边提交一次,
    就把 SqliteSaver 正在写的事务结掉了,它再 commit 就抛
    "cannot commit - no transaction is active",整轮直接失败。

    同一个 db 文件、两条连接,WAL 模式下互不干扰 —— 这才是正确用法。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    cp = sqlite3.connect(str(cfg.db), check_same_thread=False)
    cp.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(cp)
    setup = getattr(saver, "setup", None)
    if callable(setup):
        setup()
    return saver


def _build_agents(cfg, conn, ctx):
    if cfg.skills.enabled:
        skills_mod.ensure_dir(cfg)
    saver = checkpointer(cfg)
    agents = {name: graph.build(cfg, saver, ctx, bot=name) for name in cfg.bots}
    return agents, saver


def build_agents(cfg, conn) -> dict:
    """每个配置过的 bot 一张图,共用同一个 checkpointer。

    共用 checkpointer 是交接有上下文的关键:thread_id 是目标 id,
    换了 bot 也还是同一条线程,新 bot 能看见前任的全部消息。
    """
    if graph.CURRENT is None:
        graph.CURRENT = graph.RunCtx(cfg=cfg, conn=conn)
    agents, _saver = _build_agents(cfg, conn, graph.CURRENT)
    return agents


def build_agents_fresh(cfg, conn):
    """GUI 用:显式建一份新的 RunCtx 并把 saver 交出来。

    saver 攥着自己的 sqlite 连接,配置热重载时要能关掉它,
    否则 Windows 上 db 文件一直被锁着。
    """
    ctx = graph.RunCtx(cfg=cfg, conn=conn)
    agents, saver = _build_agents(cfg, conn, ctx)
    return agents, saver, ctx


def summarize(stats: dict) -> str:
    bits = []
    for key, label in (
        ("events_new", "新事件"), ("approval_replies", "审批回复"), ("goals_new", "新目标"),
        ("resumed", "续跑"), ("ran", "跑完"), ("handoffs", "交接"),
        ("interrupted", "等审批"), ("failed", "失败"),
    ):
        if stats.get(key):
            bits.append(f"{label} {stats[key]}")
    if stats.get("paused"):
        bits.append(stats["paused"])
    return " / ".join(bits)


def recover_stale_runs(cfg, conn, older_than_sec: int = 1800) -> int:
    """上次进程被 kill 时卡在 running 的目标,放回 ready。

    checkpoint 还在,下一轮 _run 会从上次停的地方接着走,不是从头开始。
    没有这一步,一次 kill -9 就能让目标永远停在 running 上。
    """
    rows = db.stale_running(conn, time.time() - older_than_sec)
    for g in rows:
        db.set_goal(conn, g["id"], status=db.READY)
        db.audit(conn, g["id"], "recovered_stale_run", "上次进程没跑完,放回队列")
    return len(rows)


def serve(cfg, conn, *, dry_run: bool = False, once: bool = False,
          web: bool = False, web_port: int = 8765) -> None:
    recovered = recover_stale_runs(cfg, conn)
    if recovered:
        print(f"[dot] 收回 {recovered} 个上次没跑完的目标")
    if web:
        if once:
            print("[dot] --once 配 --web 没意义:跑完就退,页面也跟着没了。"
                  "要看活动视图请用 python -m dots serve --web", file=sys.stderr)
        else:
            from dots import web as web_mod

            web_mod.start(cfg.db, port=web_port)
    agents = build_agents(cfg, conn)
    if once:
        stats = tick(cfg, conn, agents, dry_run=dry_run)
        print(summarize(stats) or "无事发生")
        return

    print(f"[dot] 起来了。tick={cfg.tick_seconds}s  工作区={cfg.workspace}")
    print(f"[dot] bot:{', '.join(sorted(cfg.bots))}"
          + (f"  技能:{len(skills_mod.scan(cfg))} 个" if cfg.skills.enabled else ""))
    if not cfg.smtp.usable:
        print("[dot] 没配 [smtp],审批会退回 CLI:python -m dots approve <token>")
    while True:
        try:
            stats = tick(cfg, conn, agents, dry_run=dry_run)
            line = summarize(stats)
            if line:                       # 无事发生就不打日志
                print(f"[dot] {line}")
        except KeyboardInterrupt:
            print("\n[dot] 停了。没跑完的目标留在库里,下次启动接着跑。")
            return
        except Exception:
            db.audit(conn, None, "tick_error", traceback.format_exc()[-1000:])
            print(traceback.format_exc()[-1000:], file=sys.stderr)
        time.sleep(cfg.tick_seconds)
