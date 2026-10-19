"""命令行。

CLI 只往 SQLite 写东西,不跟 daemon 通信 —— 没有 socket,没有 IPC。
`dots goal` 插一行 goals,`dots approve` 改一行 approvals,常驻进程下一轮自己就看见了。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from dots import __version__
from dots import approvals, db
from dots import config as config_mod
from dots import rules as rules_mod
from dots import skills as skills_mod
from dots.prompts import CURATE_PROMPT, LEARN_PROMPT


# 别在模块顶层导入 dots.worker / dots.graph —— 那条链
# (worker -> graph -> deepagents -> langchain -> transformers -> torch)在开发机上
# 8.7 秒,而 `dots ls` 只是读几行 SQLite。用到的地方就地导入,每处都标了为什么。


def _parser() -> argparse.ArgumentParser:
    """命令表。交互终端和命令行共用这一份,免得有两套说法。"""
    p = argparse.ArgumentParser(
        prog="dots", description="常驻主动式 agent。给一个目标,它自己在后台推。"
    )
    p.add_argument("-c", "--config", default=None,
                   help="配置文件路径(默认先找 ./dot.toml,再找 ~/.dots/dot.toml)")
    sub = p.add_subparsers(dest="cmd")

    ini = sub.add_parser("init", help="生成一份 dot.toml(自动探测模型)")
    ini.add_argument("--global", dest="global_", action="store_true",
                     help=f"写到 {config_mod.GLOBAL_PATH},之后在任何目录敲 dots 都能用")
    sub.add_parser("doctor", help="体检:配置、密钥、模型、目录、数据库")
    g = sub.add_parser("goal", help="下一个目标")
    g.add_argument("text", nargs="+")
    g.add_argument("--bot", default="dot", help="交给哪个 bot 做(默认 dot)")
    ln = sub.add_parser("learn", help="把一段材料/一套流程总结成技能")
    ln.add_argument("source", nargs="+")
    sub.add_parser("skills", help="列出技能库")
    sk = sub.add_parser("skill", help="技能管理")
    sk_sub = sk.add_subparsers(dest="skill_cmd", required=True)
    sk_show = sk_sub.add_parser("show", help="看一份技能的正文")
    sk_show.add_argument("name")
    sk_rm = sk_sub.add_parser("rm", help="删掉一份技能")
    sk_rm.add_argument("name")
    sub.add_parser("curate", help="整理技能库:合并重复、删掉没价值的")
    s = sub.add_parser("ls", help="列目标")
    s.add_argument("--status", help="只看某个状态")
    lg = sub.add_parser("log", help="活动记录")
    lg.add_argument("--goal", help="只看某个目标")
    lg.add_argument("-n", type=int, default=30)
    ap = sub.add_parser("approve", help="批准一个挂起的动作")
    ap.add_argument("token")
    rj = sub.add_parser("reject", help="拒绝一个挂起的动作")
    rj.add_argument("token")
    sub.add_parser("cost", help="token 用量")
    tk = sub.add_parser("tick", help="只跑一轮(调试用)")
    tk.add_argument("--dry-run", action="store_true")
    sv = sub.add_parser("serve", help="常驻跑起来")
    sv.add_argument("--dry-run", action="store_true", help="不真发邮件,审批打印到终端")
    sv.add_argument("--once", action="store_true", help="只跑一轮就退")
    sv.add_argument("--web", action="store_true", help="同时起只读活动视图(只听本机)")
    sv.add_argument("--web-port", type=int, default=8765)
    gu = sub.add_parser("gui", help="打开本地网页界面(聊天/配置/技能/审批)")
    gu.add_argument("--port", type=int, default=8765)
    gu.add_argument("--no-browser", action="store_true", help="只起服务,不开浏览器")
    gu.add_argument("--dry-run", action="store_true", help="不真发邮件")
    sub.add_parser("selftest", help="不联网、不调模型,跑一遍自检")
    br = sub.add_parser("bridge", help="给桌面界面用的 JSON-lines 后端(一般不用手敲)")
    # bridge 有自己的解析器(main() 里那份)。这里只是让 `dots bridge --no-loop`
    # 不被顶层当成错参数。default=SUPPRESS 是关键:subparser 的默认值会**覆盖**
    # 顶层命名空间,不压住的话 `dots -c x bridge` 里前面那个 -c 会被抹成 None。
    br.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    br.add_argument("--no-loop", action="store_true", default=argparse.SUPPRESS,
                    help="起来了先不跑 tick 循环")
    return p


def main(argv=None) -> int:
    # Windows 控制台/管道默认是 GBK,而自检和 doctor 要打 ✓ 和中文。
    # 不换编码就是 UnicodeEncodeError: 'gbk' codec can't encode '✓' ——
    # 用户拿到的是栈,不是体检结果。换不了就算了(比如 stdout 被换成了
    # 我们自己那种只有 write 的桩)。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = _parser().parse_args(argv)

    if args.cmd is None:
        return _cmd_repl(args)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "selftest":
        return _cmd_selftest()
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "bridge":
        from dots import bridge as bridge_mod   # 惰性:只有界面用得上
        argv = ["-c", args.config] if getattr(args, "config", None) else []
        if getattr(args, "no_loop", False):
            argv.append("--no-loop")
        return bridge_mod.main(argv)
    if args.cmd == "gui":
        from dots import gui as gui_mod      # 惰性:只有网页界面用得上
        return gui_mod.run_gui(
            config=args.config, port=args.port,
            open_browser=not args.no_browser, dry_run=args.dry_run,
        )

    cfg = config_mod.load(args.config)

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
    try:
        return _dispatch(args, cfg, conn)
    finally:
        # 一次性进程不关也无所谓,但交互终端每行都会走一遍这里 ——
        # 不关就是一个命令漏一个连接,顺带把 dot.db 一直锁着。
        conn.close()


def _dispatch(args, cfg, conn) -> int:
    if args.cmd == "goal":
        text = " ".join(args.text).strip()
        bot = (args.bot or "dot").strip().lower()
        if bot not in cfg.bots:
            print(f"没有叫 {bot!r} 的 bot。可用的:{', '.join(sorted(cfg.bots))}",
                  file=sys.stderr)
            return 2
        gid = db.add_goal(conn, text, origin="cli", status=db.READY, bot=bot)
        db.audit(conn, gid, "goal_created", f"[{bot}] {text[:200]}")
        print(f"{gid}  {text}")
        if bot != "dot":
            print(f"交给 {bot} 做")
        print("跑起来:python -m dots serve", file=sys.stderr)
        return 0

    if args.cmd == "learn":
        return _cmd_learn(conn, cfg, " ".join(args.source).strip())
    if args.cmd == "curate":
        return _cmd_curate(conn, cfg)
    if args.cmd == "skills":
        return _cmd_skills(cfg)
    if args.cmd == "skill":
        return _cmd_skill(cfg, args.skill_cmd, args.name)

    if args.cmd == "ls":
        return _cmd_ls(conn, args.status)
    if args.cmd == "log":
        return _cmd_log(conn, args.goal, args.n)
    if args.cmd in ("approve", "reject"):
        return _cmd_decide(conn, args.token, args.cmd)
    if args.cmd == "cost":
        return _cmd_cost(conn, cfg)
    if args.cmd == "tick":
        from dots import worker      # 惰性:要跑 agent,本来就慢
        worker.serve(cfg, conn, dry_run=args.dry_run, once=True)
        return 0
    if args.cmd == "serve":
        from dots import worker      # 惰性:要跑常驻循环,本来就慢
        worker.serve(
            cfg, conn, dry_run=args.dry_run, once=args.once,
            web=args.web, web_port=args.web_port,
        )
        return 0

    _parser().print_help()
    return 1


# ---------------------------------------------------------------- 交互终端

# 手写的,不引 pyfiglet —— 一个 banner 不值得多一个依赖。
# 六行等高,test_cli 盯着这点:粘贴时错位一格,这里就报警。
_BANNER = """
 ██████╗  ██████╗ ████████╗███████╗
 ██╔══██╗██╔═══██╗╚══██╔══╝██╔════╝
 ██║  ██║██║   ██║   ██║   ███████╗
 ██║  ██║██║   ██║   ██║   ╚════██║
 ██████╔╝╚██████╔╝   ██║   ███████║
 ╚═════╝  ╚═════╝    ╚═╝   ╚══════╝"""


def banner() -> str:
    lines = _BANNER.strip("\n").splitlines()
    assert len({len(x) for x in lines}) == 1, "banner 每行必须等宽,不然右边会歪"
    return _BANNER.strip("\n")


def _cmd_repl(args) -> int:
    """不带子命令就进这里。

    两种输入:
      /ls /goal "..." /approve  → 命令。每一行原样再走一遍 main(),
                                  所以交互里能用的和命令行**完全一样**,
                                  没有第二套命令表要维护。
      其它                      → 当成一句话发给 agent(见 _chat)。

    副作用正合意:每行重读 dot.toml,改完配置不用退出来重进。
    """
    # 重定向到文件或管道时不画 —— 那玩意儿是给人看的,不是给日志看的
    if sys.stdout.isatty():
        print(banner())
        print(f"  v{__version__} · 一个会主动发现任务、主动积累技能的智能体\n")

    print("直接打字 = 和它说话;/ 开头是命令(/help 看全部,/goal 交给后台跑)。")
    found = config_mod.resolve(args.config)
    if found is None:
        print("(没找到配置。`dots init --global` 配一次,以后在哪都能用)")
    elif found != Path(config_mod.DEFAULT_FILENAME):
        print(f"(用的是 {found})")
    print()

    session = None                   # 第一次说话才建,省掉不聊天时的模型开销
    try:
        while True:
            try:
                line = input("dots> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue

            if not line.startswith("/"):
                if session is None:
                    try:
                        session = _new_chat(args)
                    except SystemExit as e:
                        print(e, file=sys.stderr)
                        continue
                    if session is None:
                        continue
                _ask_agent(session, line)
                continue

            try:
                argv = shlex.split(line[1:])
            except ValueError as e:          # 引号没配对
                print(f"读不懂这行:{e}", file=sys.stderr)
                continue
            if not argv:
                continue
            if argv[0] in ("exit", "quit", "q"):
                return 0
            if argv[0] == "bridge":
                # bridge 要独占 stdin,在这儿跑会把 input() 饿死 ——
                # 之后每行都被它当成「不是合法 JSON」,直到 EOF 才回到 dots>。
                print("bridge 是给桌面界面用的,不能在交互终端里跑。", file=sys.stderr)
                continue
            if argv[0] in ("help", "?"):
                _parser().print_help()
                print("\n交互终端里,上面每个命令前面加 /(如 /ls、/goal \"...\")。")
                continue

            # 没显式指定就不传 -c —— 让每行自己重新找一遍,
            # 这样在终端里 init 完,下一条命令就能用上,不用退出来重进。
            base = ["-c", args.config] if args.config else []
            try:
                main([*base, *argv])
            except KeyboardInterrupt:
                print("\n(打断,回到终端)")     # 比如 serve 跑着的时候 Ctrl-C
            except SystemExit as e:           # 配置坏了、argparse 报错
                if e.code not in (0, None):
                    print(f"(退出码 {e.code})", file=sys.stderr)
    finally:
        if session is not None:
            _close_chat(session)


def _new_chat(args):
    """建对话用的一张图。建不出来返回 None —— 那样 / 命令还能接着用。"""
    from dots import worker          # 惰性:只有真聊天才需要 langchain

    cfg = config_mod.load(args.config)
    problems = rules_mod.validate(cfg.rules)
    if problems:
        for x in problems:
            print(f"[dots] 配置有问题:{x}", file=sys.stderr)
        return None
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db)
    db.init(conn)
    try:
        agents, saver = worker.chat_agents(cfg, conn)
    except BaseException:
        conn.close()
        raise
    return cfg, agents, conn, saver


def _close_chat(session) -> None:
    """两条连接都要关:应用库一条,checkpointer 自己一条。

    漏掉后者,Windows 上这段时间 dot.db 一直锁着,临时目录删不掉(踩过两次)。
    """
    _, _, conn, saver = session
    conn.close()
    saver.conn.close()


def _ask_agent(session, line: str) -> None:
    """一句话发给 agent,把回答打出来。

    审批在这里当场问 —— 后台是靠邮件/`/approve`,对话里当场问更顺。
    """
    from dots import worker          # 惰性:只有真发消息才需要 langchain

    cfg, agents, conn, _ = session

    def decide(payload: dict) -> bool:
        args = payload.get("args") or {}
        print(f"\n  它想干这个:{payload.get('action')}"
              f"  {json.dumps(args, ensure_ascii=False)[:300]}")
        try:
            return input("  批准?(y/N) ").strip().lower() in ("y", "yes", "是", "同意")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    try:
        reply, _ = worker.chat_send(agents, cfg, conn, line, decide)
    except KeyboardInterrupt:
        # 图停在半路,checkpoint 还在,下一句接着走
        print("\n(打断。刚才那句的上下文还在。)")
        return
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return
    print(reply)


# ------------------------------------------------------------------ 子命令

def _cmd_init(args) -> int:
    # 三种落点:--global 写 ~/.dots,显式 -c 写那儿,都没有就写当前目录
    if args.global_:
        dest = config_mod.GLOBAL_PATH
    elif args.config:
        dest = Path(args.config)
    else:
        dest = Path(config_mod.DEFAULT_FILENAME)

    if dest.exists():
        print(f"{dest} 已经存在,没动它。体检一下:dots doctor")
        return 0

    text = (Path(__file__).parent / "template.toml").read_text(encoding="utf-8")
    preset = config_mod.detect_model()
    if preset:
        name, base_url, key_env, _ = preset
        body = [f'name = "{name}"']
        body.append(f'base_url = "{base_url}"' if base_url else '# base_url = ""')
        body.append(f'api_key_env = "{key_env}"' if key_env else '# api_key_env = ""')
        body.append("")
        text = _replace_section_body(text, "model", "\n".join(body))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")

    print(f"写好了 {dest}")
    if preset:
        print(f"探测到环境里有 {preset[2]},[model] 已经按它配好 —— 不用再动。")
    else:
        print("没探测到 API key。先 export 一个再重跑 init,或者手填 [model] 段。")

    if args.global_:
        print("之后在任何目录敲 dots 都用这一份,数据在 "
              f"{dest.parent}/(workspace、dot.db)。")
    else:
        print(f"这份只在这个目录生效(数据放 {dest.parent})。"
              f"\n想 cd 到哪都能用:`dots init --global`")

    print(
        f"\n下一步:\n"
        f"  1. dots doctor            体检\n"
        f"  2. dots goal \"把 inbox 里的东西整理成一份报告\"\n"
        f"  3. dots serve --web\n"
        f"\n不带子命令直接敲 dots 会进交互终端,上面这些命令换个方式敲。\n"
    )
    return 0


def _replace_section_body(text: str, section: str, body: str) -> str:
    """把 [section] 下面的内容整段换掉,直到下一个 [ 开头的行。

    比按行字符串替换稳:模板里的注释行随便改,这里都不会错位。
    """
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == f"[{section}]":
            out.append(lines[i])
            out.append(body)
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n"


# --------------------------------------------------------------- 技能

def _cmd_learn(conn, cfg, source: str) -> int:
    if not cfg.skills.enabled:
        print("[skills] enabled = false,先打开它。", file=sys.stderr)
        return 2
    text = LEARN_PROMPT.format(source=source, dir=cfg.skills.dir)
    gid = db.add_goal(conn, text, origin="cli:learn", status=db.READY)
    db.audit(conn, gid, "learn", source[:200])
    print(f"{gid}  学:{source[:70]}")
    print("跑起来:python -m dots serve  (或者 python -m dots tick)", file=sys.stderr)
    return 0


def _cmd_curate(conn, cfg) -> int:
    if not cfg.skills.enabled:
        print("[skills] enabled = false,先打开它。", file=sys.stderr)
        return 2
    found = skills_mod.scan(cfg)
    if len(found) < 2:
        print(f"技能库只有 {len(found)} 份,没什么好整理的。")
        return 0
    text = CURATE_PROMPT.format(dir=cfg.skills.dir)
    gid = db.add_goal(conn, text, origin="cli:curate", status=db.READY)
    db.audit(conn, gid, "curate", f"{len(found)} 份技能")
    print(f"{gid}  整理 {len(found)} 份技能")
    print("提醒:它会真的删/改技能文件。先 python -m dots skills 看一眼现状。",
          file=sys.stderr)
    return 0


def _cmd_skills(cfg) -> int:
    found = skills_mod.scan(cfg)
    if not found:
        print(f"技能库是空的({cfg.workspace / cfg.skills.dir})。")
        print('攒一份:python -m dots learn "..."')
        return 0
    rows = [(s.name, _w(s.description) and f"{len(s.description):>3}", s.description)
            for s in found]
    print(_table(rows, ("NAME", "LEN", "DESCRIPTION")))
    print(f"\n{len(found)} 份。看正文:python -m dots skill show <name>"
          f"   整理:python -m dots curate")
    return 0


def _cmd_skill(cfg, action: str, name: str) -> int:
    found = {s.name: s for s in skills_mod.scan(cfg)}
    skill = found.get(name)
    if skill is None:
        print(f"没有叫 {name!r} 的技能。" + (f"有的是:{', '.join(found)}" if found else ""),
              file=sys.stderr)
        return 1
    if action == "show":
        print(skill.path.read_text(encoding="utf-8", errors="replace"))
        return 0
    if action == "rm":
        skills_mod.remove(cfg, name)
        print(f"删掉了 {name}({skill.path})")
        return 0
    return 1


# --------------------------------------------------------------- 体检

def _cmd_doctor(args) -> int:
    """一条一条查,每条都给出能照做的下一步。"""
    ok_all = True

    def check(label: str, ok: bool, detail: str = "", fix: str = "") -> None:
        nonlocal ok_all
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            ok_all = False
            if fix:
                print(f"      -> {fix}")

    dest = config_mod.resolve(args.config)
    print(f"dots 体检({dest or args.config or '没找到配置'})\n")

    if dest is None:
        check("配置文件", False, "当前目录和 ~/.dots 下都没有",
              "dots init --global(到处都能用)或 dots init(只在这个目录)")
        return 1
    check("配置文件", True, str(dest.resolve()))

    try:
        cfg = config_mod.load(args.config)
    except SystemExit as e:
        check("读配置", False, str(e))
        return 1
    check("读配置", True)

    problems = rules_mod.validate(cfg.rules)
    check("审批规则", not problems,
          f"{len(cfg.rules)} 条" if not problems else problems[0],
          "改 [approval.rules] 的值,只能是 allow / ask / deny")

    try:
        from dots import graph as graph_mod   # 惰性:doctor 要真建一个模型,这条路径本来就慢
    except ImportError as e:
        # 依赖装坏时 doctor 正是用户会来敲的那条命令,它必须能说出「✗ 依赖 …」
        # 而不是甩一屏栈。ImportError 和 SystemExit 没有继承关系,混在一起就漏了。
        check("依赖", False, f"{type(e).__name__}: {e}",
              "依赖没装全:pip install -e .(或 pip install --upgrade dots-agent)")
    else:
        try:
            graph_mod.build_model(cfg)
            check("模型", True, cfg.model.name)
        except SystemExit as e:
            check("模型", False, str(e).splitlines()[0],
                  "填 [model];第三方端点要 name / base_url / api_key_env 三件套")

    try:
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        probe = cfg.workspace / ".dot-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        check("工作区可写", True, str(cfg.workspace))
    except OSError as e:
        check("工作区可写", False, str(e), f"检查 {cfg.workspace} 的权限")

    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        version = db.meta_get(conn, "schema_version", "?")
        n_goals = len(db.list_goals(conn, limit=1000))
        check("数据库", True, f"schema v{version},{n_goals} 个目标")
    except Exception as e:
        check("数据库", False, f"{type(e).__name__}: {e}",
              f"删掉 {cfg.db} 重来(会丢历史)")
        conn = None

    enabled = [n for n, s in (("fs", cfg.sources.fs), ("imap", cfg.sources.imap),
                              ("ical", cfg.sources.ical)) if s.enabled]
    check("事件源", bool(enabled),
          ", ".join(enabled) if enabled else "一个都没开",
          "至少要开一个,否则没有东西能主动唤醒它")

    if cfg.smtp.usable:
        check("审批通道", True, f"邮件 -> {cfg.smtp.to}")
    else:
        print("  ! 审批通道  没配 [smtp] —— 审批只能走 CLI(dots approve),不算错")

    if cfg.skills.enabled:
        n = len(skills_mod.scan(cfg))
        check("技能库", True, f"{n} 份  {cfg.workspace / cfg.skills.dir}")
    else:
        print("  ! 技能库  已关闭([skills] enabled = false)")

    check("bot", True, ", ".join(sorted(cfg.bots)))
    if cfg.daily_token_budget:
        check("成本上限", True, f"{cfg.daily_token_budget:,} token/天")
    else:
        print("  ! 成本上限  没设 —— 先跑一天再用 dots cost 看真实数字")

    print()
    if ok_all:
        print("可以跑:python -m dots serve --web")
    else:
        print("上面标 ✗ 的先修掉。")
    return 0 if ok_all else 1


def _cmd_ls(conn, status) -> int:
    goals = db.list_goals(conn, status, limit=100)
    if not goals:
        print('还没有目标。python -m dots goal "..." 下一个。')
        return 0

    # 等审批的目标直接把编号摊在列表里 —— 不然你还得去 log 里翻,
    # 或者更糟:自己写 SQL 查。
    waiting = {a["goal_id"]: a["token"] for a in db.pending_approvals(conn)}
    rows = [
        (g["id"], g["status"], waiting.get(g["id"], ""), _n(g["tokens"]),
         _ago(g["updated_at"]), g["text"][:44])
        for g in goals
    ]
    print(_table(rows, ("ID", "STATUS", "TOKEN", "TOKENS", "UPDATED", "TEXT")))
    if waiting:
        tokens = " ".join(sorted(waiting.values()))
        print(f"\n{len(waiting)} 个在等你。批准:dots approve <编号>   拒绝:dots reject <编号>")
        print(f"编号:{tokens}")
    return 0


def _cmd_log(conn, goal_id, n) -> int:
    rows_raw = db.recent_audit(conn, n, goal_id)
    if not rows_raw:
        print("还没有活动记录。")
        return 0
    rows = [
        (_clock(r["ts"]), r["goal_id"] or "-", r["kind"], _clip(r["detail"], 70))
        for r in reversed(rows_raw)
    ]
    print(_table(rows, ("TIME", "GOAL", "KIND", "DETAIL")))
    return 0


def _cmd_decide(conn, token: str, cmd: str) -> int:
    decision = approvals.APPROVED if cmd == "approve" else approvals.REJECTED
    row = db.get_approval(conn, token)
    if row is None:
        print(f"没有编号 {token} 的待批动作。python -m dots log 看一眼。", file=sys.stderr)
        return 1
    if not db.resolve_approval(conn, token, decision):
        print(f"{token} 已经批过了({row['status']}),这次不算。", file=sys.stderr)
        return 1
    db.audit(conn, row["goal_id"], "approval_cli", f"{token} -> {decision}")
    print(f"{token} -> {decision}。目标 {row['goal_id']} 下一轮 tick 会接着跑。")
    if cmd == "approve":
        print(f"  它要做的是:{row['tool']} {row['args'][:200]}")
    return 0


def _cmd_cost(conn, cfg) -> int:
    day = db.tokens_since(conn, db.local_midnight())
    total = db.tokens_since(conn, 0)
    print(f"今日  {_n(day)}")
    print(f"累计  {_n(total)}")
    if cfg.daily_token_budget:
        print(f"上限  {_n(cfg.daily_token_budget)}/天")
    else:
        print("上限  没设([safety] daily_token_budget = 0)")
    top = conn.execute(
        "SELECT id, text, tokens FROM goals WHERE tokens > 0 ORDER BY tokens DESC LIMIT 10"
    ).fetchall()
    if top:
        print()
        print(_table(
            [(g["id"], _n(g["tokens"]), g["text"][:48]) for g in top],
            ("GOAL", "TOKENS", "TEXT"),
        ))
    return 0


def _cmd_selftest() -> int:
    """不联网、不调模型。

    分两档:
      内置的几条 —— 永远跑,装完 pip 的人也能验证依赖和纯逻辑没坏。
      完整套件 —— 只在源码仓库里有(wheel 不带 tests/),含假 IMAP 服务器、
                    交接、审批闭环那些。
    硬要两层都跑会逼着把 tests/ 打进 wheel,那不值得:
    用户要的是「我这套装得对不对」,开发者要的才是那 20 多条断言。
    """
    from dots import sources

    print("== rules ==")
    rules_mod.demo()
    print("== sources(文件源)==")
    sources.demo()
    print("== approvals ==")
    approvals.demo()
    print("== skills ==")
    skills_mod.demo()

    try:
        from tests import test_bridge, test_cli, test_loop, test_sources, test_web
    except ImportError:
        print("\n内置自检通过。")
        print("完整套件(本地假 IMAP 服务器 / 假模型跑 tick / 活动视图)在源码仓库里:")
        print("  git clone https://github.com/duyongan/dots-agent")
        print("  cd dots-agent && python -m dots selftest")
        return 0

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
    print("\n自检通过。")
    return 0


# -------------------------------------------------------------------- 工具

def _table(rows, headers) -> str:
    widths = [_w(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], _w(c))
    out = ["  ".join(str(h).ljust(widths[i] + _w(h) - len(str(h))) for i, h in enumerate(headers))]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def _w(s) -> int:
    """终端里中文占两格。不算这个,表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(value, width: int) -> str:
    s = str(value)
    return s + " " * max(0, width - _w(s))


def _clip(s, n: int) -> str:
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _n(v) -> str:
    return f"{int(v):,}"


def _clock(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def _ago(ts) -> str:
    d = max(0, int(datetime.now().timestamp() - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


if __name__ == "__main__":
    raise SystemExit(main())
