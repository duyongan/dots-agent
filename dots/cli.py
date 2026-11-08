"""命令行。

CLI 只往 SQLite 写东西,不跟 daemon 通信 —— 没有 socket,没有 IPC。
`dots goal` 插一行 goals,`dots approve` 改一行 approvals,常驻进程下一轮自己就看见了。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
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

    mc = sub.add_parser("mcp", help="MCP 接入:飞书 / Jira / Sigma / GitLab")
    mc_sub = mc.add_subparsers(dest="mcp_cmd", required=True)
    mc_sub.add_parser("list", help="看接了哪些外部系统、缺什么")
    mc_add = mc_sub.add_parser("add", help="把一家现成的 MCP 配置写进 dot.toml")
    from dots import mcp as _mcp_mod          # 纯配置模块,不引 langchain
    mc_add.add_argument("preset", choices=sorted(_mcp_mod.PRESETS))
    mc_add.add_argument("--force", action="store_true", help="已存在也覆盖")

    on = sub.add_parser("ontology", help="本体库:采集元数据 / 手改 / 派生文档和代码")
    on_sub = on.add_subparsers(dest="ontology_cmd", required=True)
    on_sub.add_parser("init", help="建一份空的 ontology.json")
    on_show = on_sub.add_parser("show", help="看实体列表,或某个实体的完整定义")
    on_show.add_argument("entity", nargs="?")
    on_sub.add_parser("edit", help="用编辑器手改本体库,存完自动校验并派生")
    on_sub.add_parser("validate", help="校验本体库")
    on_sync = on_sub.add_parser("sync", help="从公司库采元数据并合并进本体库")
    on_sync.add_argument("source", nargs="?", help="只采某个数据源(默认全部)")
    on_sub.add_parser("apply", help="按本体库重新生成派生文档和代码")
    on_sub.add_parser("status", help="派生层是否跟本体库一致")

    sd = sub.add_parser("sdlc", help="全流程流水线:需求→原型→开发→测试→修复")
    sd_sub = sd.add_subparsers(dest="sdlc_cmd", required=True)
    sd_new = sd_sub.add_parser("new", help="开一条流水线")
    sd_new.add_argument("text", nargs="+", help="需求原文")
    sd_new.add_argument("--title", default="", help="标题(默认取需求第一行)")
    sd_new.add_argument("--track", default="feature", choices=("feature", "bugfix"))
    sd_new.add_argument("--jira", default="", help="关联的 Jira 单号")
    sd_sub.add_parser("ls", help="列出流水线")
    sd_show = sd_sub.add_parser("show", help="看一条流水线的详情")
    sd_show.add_argument("id")
    sd_sub.add_parser("sync", help="把本体库变更同步进所有流水线文档")
    sd_adv = sd_sub.add_parser("advance", help="人工跳过当前阶段(卡住时用)")
    sd_adv.add_argument("id")
    sd_adv.add_argument("--note", default="人工跳过")

    bg = sub.add_parser("bugs", help="缺陷台账(微信群报的 BUG 都在这儿)")
    bg_sub = bg.add_subparsers(dest="bugs_cmd")
    bg_ls = bg_sub.add_parser("ls", help="列缺陷")
    bg_ls.add_argument("--status", help="只看某个状态(open/fixing/fixed/closed)")
    bg_show = bg_sub.add_parser("show", help="看一条缺陷")
    bg_show.add_argument("id")
    bg_close = bg_sub.add_parser("close", help="关掉一条缺陷")
    bg_close.add_argument("id")
    bg_fix = bg_sub.add_parser("fix", help="给一条或多条缺陷开修复流水线")
    bg_fix.add_argument("ids", nargs="+")
    bg_fix.add_argument("--title", default="")

    wx = sub.add_parser("wechat", help="微信群监控(spool / webhook)")
    wx_sub = wx.add_subparsers(dest="wechat_cmd", required=True)
    wx_in = wx_sub.add_parser("ingest", help="手动灌一条消息(补录/联调)")
    wx_in.add_argument("text", nargs="+")
    wx_in.add_argument("--group", default="")
    wx_in.add_argument("--sender", default="")
    wx_hook = wx_sub.add_parser("hook", help="起本机 webhook,接收桥推来的消息")
    wx_hook.add_argument("--port", type=int, default=8790)
    wx_sub.add_parser("status", help="spool 在哪、读到哪了")

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

    if args.cmd == "mcp":
        return _cmd_mcp(args, cfg)
    if args.cmd == "ontology":
        return _cmd_ontology(args, cfg)
    if args.cmd == "sdlc":
        return _cmd_sdlc(args, cfg, conn)
    if args.cmd == "bugs":
        return _cmd_bugs(args, cfg, conn)
    if args.cmd == "wechat":
        return _cmd_wechat(args, cfg)

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

def _cmd_mcp(args, cfg) -> int:
    from dots import mcp as mcp_mod

    if args.mcp_cmd == "add":
        dest = config_mod.resolve(args.config)
        if dest is None:
            print("先 dots init 生成一份配置,再往里面加 MCP。", file=sys.stderr)
            return 2
        mcp_mod.add_preset(dest, args.preset, force=args.force)
        preset = mcp_mod.PRESETS[args.preset]
        print(f"写好了 {dest} 的 [mcp.servers.{args.preset}]({preset['title']})。")
        if preset.get("pass_env"):
            print("凭据放环境变量,别写进配置文件:")
            for name in preset["pass_env"]:
                print(f"  export {name}=...   # Windows: $env:{name}=\"...\"")
        print(f"提示:{preset['note']}")
        print("检查:dots mcp list")
        return 0

    if not cfg.mcp.servers:
        print("还没配 MCP server。接入一家:dots mcp add jira|feishu|gitlab|sigma")
        return 0
    try:
        mcp_mod.adapter()
        has_adapter = True
    except ImportError:
        has_adapter = False

    rows = []
    for name, server in cfg.mcp.servers.items():
        preset = mcp_mod.PRESETS.get(name)
        title = preset["title"] if preset else ""
        missing = [x for x in (server.pass_env or []) if not os.environ.get(x)]
        if not server.enabled:
            state = "关"
        elif server.transport == "stdio" and not server.command:
            state = "配置不全:缺 command"
        elif server.transport != "stdio" and not server.url:
            state = "配置不全:缺 url"
        elif missing:
            state = "缺环境变量:" + ",".join(missing)
        else:
            state = "就绪"
        rows.append((name, title, server.transport, state))
    print(_table(rows, ("NAME", "系统", "TRANSPORT", "状态")))
    if not has_adapter:
        print("\n没装 langchain-mcp-adapters,工具还挂不上:pip install 'dots-agent[mcp]'")
    elif not cfg.mcp.enabled:
        print("\n[mcp] enabled = false —— 配了也不会连。")
    print("\n工具名带前缀(jira__create_issue),审批规则可以按系统写通配:jira__* = \"ask\"")
    return 0


def _cmd_ontology(args, cfg) -> int:
    from dots import ontology as ont
    from dots import sdlc as sdlc_mod

    if not cfg.ontology.enabled:
        print("本体库被关掉了([ontology] enabled = false)。", file=sys.stderr)
        return 2

    if args.ontology_cmd == "init":
        p = ont.path(cfg)
        if p.exists():
            print(f"{p} 已经存在,没动它。看内容:dots ontology show")
            return 0
        ont.save(cfg, ont.empty(), note="init")
        print(f"建好了 {p}。")
        print("有数据库:先在 [ontology.datasources.*] 配 dsn_env,再 dots ontology sync")
        print("纯手工:直接 dots ontology edit 写实体")
        return 0

    if args.ontology_cmd == "show":
        doc = ont.load(cfg)
        if args.entity:
            ent = ont.find_entity(doc, args.entity)
            if ent is None:
                print(f"没有叫 {args.entity!r} 的实体。", file=sys.stderr)
                return 1
            print(json.dumps(ent, ensure_ascii=False, indent=2))
            return 0
        ents = doc.get("entities") or []
        if not ents:
            print(f"本体库是空的({ont.path(cfg)})。")
            print("dots ontology sync(从库采)或 dots ontology edit(手写)")
            return 0
        rows = [
            (e.get("id"), e.get("label"), len(e.get("fields") or []),
             (e.get("source") or {}).get("table") or "-",
             "改过" if e.get("curated") else (e.get("status") or "draft"),
             "库中已无" if e.get("missing") else "")
            for e in ents
        ]
        print(_table(rows, ("ID", "名称", "字段", "物理表", "状态", "备注")))
        print(f"\n{len(ents)} 个实体 · 指纹 {ont.digest(doc)} · {ont.path(cfg)}")
        print("看某个:dots ontology show <id>    手改:dots ontology edit")
        return 0

    if args.ontology_cmd == "validate":
        problems = ont.validate(ont.load(cfg))
        if problems:
            print("校验不过:")
            for x in problems:
                print(f"  - {x}")
            return 1
        print("本体库校验通过。")
        return 0

    if args.ontology_cmd == "edit":
        p = ont.path(cfg)
        if not p.exists():
            ont.save(cfg, ont.empty(), note="init")
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or (
            "notepad" if os.name == "nt" else "vi")
        print(f"编辑 {p}({editor})。存盘后会自动校验并重新生成派生文件。")
        try:
            subprocess.call([editor, str(p)])
        except OSError as e:
            print(f"打不开编辑器({e})。直接改这个文件,然后跑 dots ontology apply:\n  {p}",
                  file=sys.stderr)
            return 1
        problems = ont.validate(ont.load(cfg))
        if problems:
            print("改完的 JSON 校验不过,派生文件没有动:", file=sys.stderr)
            for x in problems:
                print(f"  - {x}", file=sys.stderr)
            return 1
        return _apply_ontology(cfg, note="edit")

    if args.ontology_cmd == "sync":
        return _sync_ontology(cfg, args.source)

    if args.ontology_cmd == "apply":
        return _apply_ontology(cfg, note="apply")

    if args.ontology_cmd == "status":
        st = ont.status(cfg)
        if st["synced"]:
            print(f"派生层是最新的(指纹 {st['digest']})。")
            return 0
        print(f"派生层不是最新的:{st['reason']}")
        for rel in st["changed"][:12]:
            print(f"  待更新  {rel}")
        for rel in st["missing"][:12]:
            print(f"  缺失    {rel}")
        for rel in st["hand_edited"][:12]:
            print(f"  被手改  {rel}")
        print("跑:dots ontology apply")
        return 1
    return 2


def _apply_ontology(cfg, *, note: str = "") -> int:
    from dots import ontology as ont
    from dots import sdlc as sdlc_mod

    doc = ont.load(cfg)
    problems = ont.validate(doc)
    if problems:
        print("本体库校验不过,没有生成任何东西:", file=sys.stderr)
        for x in problems:
            print(f"  - {x}", file=sys.stderr)
        return 1
    res = ont.apply(cfg, doc)
    docs = sdlc_mod.sync_docs(cfg, doc)
    print(f"派生文件 {len(res['files'])} 个:新写/更新 {len(res['written'])},"
          f"删掉 {len(res['removed'])}。指纹 {res['digest']}")
    for rel in res["written"][:10]:
        print(f"  + {rel}")
    print(f"流水线文档同步:{docs['pipelines']} 条流水线,改了 {len(docs['changed'])} 个文件")
    for rel in docs["changed"][:10]:
        print(f"  ~ {rel}")
    print(f"根目录:{cfg.workspace / cfg.ontology.derived}")
    return 0


def _sync_ontology(cfg, source: str | None) -> int:
    from dots import ontology as ont
    from dots import sdlc as sdlc_mod

    if not cfg.ontology.datasources:
        print("没配数据源。在 dot.toml 里加一段:", file=sys.stderr)
        print("  [ontology.datasources.crm]\n  kind = \"postgres\"\n"
              "  dsn_env = \"DOT_CRM_DSN\"", file=sys.stderr)
        return 2
    names = [source] if source else list(cfg.ontology.datasources)
    doc = ont.load(cfg)
    failed = 0
    for name in names:
        ds = cfg.ontology.datasources.get(name)
        if ds is None:
            print(f"没有叫 {name!r} 的数据源。可用的:{', '.join(cfg.ontology.datasources)}",
                  file=sys.stderr)
            failed += 1
            continue
        try:
            raws = ont.introspect(ds)
        except Exception as e:                      # noqa: BLE001 - 一个库连不上不该中断全部
            print(f"{name} 采集失败:{type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue
        stats = ont.merge(doc, name, raws, ds=ds)
        print(f"{name}({ds.kind}):表/集合 {len(raws)},新增实体 {len(stats['added'])},"
              f"更新 {len(stats['updated'])},库里已无 {len(stats['missing'])}")
        for line in stats["conflicts"][:5]:
            print(f"  ! {line}")
    if not doc.get("entities"):
        print("一个实体都没采到(检查 schema / include / exclude)。", file=sys.stderr)
        return 1
    ont.save(cfg, doc, note=f"sync:{','.join(names)}")
    res = ont.apply(cfg, doc)
    docs = sdlc_mod.sync_docs(cfg, doc)
    print(f"本体库:{len(doc['entities'])} 个实体,指纹 {res['digest']}")
    print(f"派生文件更新 {len(res['written'])} 个;流水线文档改了 {len(docs['changed'])} 个")
    print("下一步:dots ontology status / dots sdlc sync")
    return 1 if failed else 0


def _cmd_sdlc(args, cfg, conn) -> int:
    from dots import sdlc as sdlc_mod

    if not cfg.sdlc.enabled:
        print("流水线被关掉了([sdlc] enabled = false)。", file=sys.stderr)
        return 2

    if args.sdlc_cmd == "new":
        text = " ".join(args.text).strip()
        external = {"jira": args.jira} if args.jira else {}
        p = sdlc_mod.create(cfg, conn, text, title=args.title,
                            track=args.track, external=external)
        print(f"{p['id']}  {p['title']}")
        print(f"目录:{cfg.sdlc.dir}/{p['slug']}  轨迹:{p['track']}  首阶段:{p['stage']}")
        print("跑起来:python -m dots serve(或 python -m dots tick)")
        return 0

    if args.sdlc_cmd == "ls":
        rows = [p for p in sdlc_mod.list_pipelines(cfg)]
        if not rows:
            print('还没有流水线。开一条:dots sdlc new "把订单导出成 CSV"')
            return 0
        print(_table(
            [(p.get("id"), p.get("status"), p.get("track"), p.get("stage"),
              str(p.get("title"))[:40], (p.get("updated_at") or "")[:16])
             for p in rows],
            ("ID", "STATUS", "TRACK", "STAGE", "TITLE", "UPDATED"),
        ))
        return 0

    if args.sdlc_cmd == "show":
        p = sdlc_mod.load(cfg, args.id)
        if p is None:
            print(f"没有 {args.id!r} 这条流水线。dots sdlc ls 看看。", file=sys.stderr)
            return 1
        print(f"{p['id']}  {p['title']}  [{p['track']}/{p['status']}]")
        print(f"目录:{cfg.sdlc.dir}/{p['slug']}  当前阶段:{p['stage']}")
        print(f"Jira:{p.get('external', {}).get('jira') or '-'}"
              f"  修复轮次:{p['rounds'].get('fix', 0)}/{cfg.sdlc.max_fix_rounds}")
        rows = []
        for stage in sdlc_mod.TRACKS.get(p["track"], []):
            rec = p["stages"].get(stage) or {}
            miss = sdlc_mod.missing_artifacts(cfg, p, stage) if stage == p["stage"] else []
            rows.append((
                stage, sdlc_mod.STAGES[stage]["title"], rec.get("status") or "pending",
                rec.get("goal_id") or "-", str(rec.get("attempts") or 0),
                "; ".join(miss)[:40],
            ))
        print(_table(rows, ("STAGE", "名称", "状态", "GOAL", "次", "缺什么")))
        hist = (p.get("history") or [])[-6:]
        if hist:
            print("\n最近:")
            for h in hist:
                print(f"  {h.get('at', '')[:16]}  {h.get('event')}  "
                      f"{h.get('stage', '')} {h.get('next', '')}")
        return 0

    if args.sdlc_cmd == "sync":
        return _apply_ontology(cfg, note="sdlc-sync")

    if args.sdlc_cmd == "advance":
        p = sdlc_mod.load(cfg, args.id)
        if p is None:
            print(f"没有 {args.id!r} 这条流水线。", file=sys.stderr)
            return 1
        print(sdlc_mod.advance_manual(cfg, conn, p, note=args.note))
        return 0
    return 2


def _cmd_bugs(args, cfg, conn) -> int:
    from dots import bugs as bugs_mod
    from dots import sdlc as sdlc_mod

    cmd = getattr(args, "bugs_cmd", None) or "ls"

    if cmd == "ls":
        rows = db.list_bugs(conn, getattr(args, "status", None), limit=50)
        if not rows:
            print("台账是空的。微信群报了 BUG 会自动进来;也可以 dots wechat ingest 试一条。")
            return 0
        print(_table(
            [(r["id"], r["severity"], r["status"], r["count"],
              (r["channel"] + ("/" + r["group_name"] if r["group_name"] else "")),
              _ago(r["last_seen"]), r["title"][:40])
             for r in rows],
            ("ID", "SEV", "STATUS", "N", "来源", "LAST", "TITLE"),
        ))
        open_n = len(db.open_bugs(conn, limit=500))
        print(f"\n{len(rows)} 条;还没修完 {open_n} 条。"
              f" 看详情:dots bugs show <id>   开流水线:dots bugs fix <id>")
        return 0

    if cmd == "show":
        row = db.get_bug(conn, args.id)
        if row is None:
            print(f"没有 {args.id!r} 这条缺陷。", file=sys.stderr)
            return 1
        print(json.dumps(dict(row), ensure_ascii=False, indent=2, default=str))
        return 0

    if cmd == "close":
        row = db.get_bug(conn, args.id)
        if row is None:
            print(f"没有 {args.id!r} 这条缺陷。", file=sys.stderr)
            return 1
        db.set_bug(conn, args.id, status=db.BUG_CLOSED)
        db.audit(conn, row["goal_id"], "bug_closed", args.id)
        print(f"{args.id} 已关闭。")
        return 0

    if cmd == "fix":
        rows, missing = [], []
        for bid in args.ids:
            row = db.get_bug(conn, bid)
            (rows if row else missing).append(row or bid)
        if missing:
            print(f"不存在的缺陷:{', '.join(map(str, missing))}", file=sys.stderr)
        if not rows:
            return 1
        if not cfg.sdlc.enabled:
            print("流水线关了,没法自动修。打开 [sdlc] enabled 再试。", file=sys.stderr)
            return 2
        title = args.title or f"[BUG] {rows[0]['title']}"
        p = sdlc_mod.create(
            cfg, conn, bugs_mod.requirement_text(rows), title=title,
            track="bugfix", bug_ids=[r["id"] for r in rows],
        )
        for row in rows:
            db.set_bug(conn, row["id"], status=db.BUG_FIXING, pipeline_id=p["id"])
        print(f"{p['id']}  {p['title']}")
        print(f"修 {len(rows)} 条缺陷,目录 {cfg.sdlc.dir}/{p['slug']}。跑起来:dots serve")
        return 0
    return 2


def _cmd_wechat(args, cfg) -> int:
    from dots import wechat as wechat_mod

    if args.wechat_cmd == "ingest":
        text = " ".join(args.text).strip()
        p = wechat_mod.append(cfg, group=args.group, sender=args.sender or "cli", text=text)
        print(f"写进 {p}")
        print("下一轮 tick 会读它;先看会不会被认成 BUG:dots bugs")
        return 0

    if args.wechat_cmd == "hook":
        wechat_mod.serve_forever(cfg, port=args.port)
        return 0

    if args.wechat_cmd == "status":
        sub = cfg.sources.wechat
        p = wechat_mod.spool_path(cfg)
        size = p.stat().st_size if p.exists() else 0
        print(f"源:      {'开' if sub.enabled else '关'}([sources.wechat] enabled)")
        print(f"spool:   {p}({size} 字节)")
        print(f"读到:    {db_cursor(cfg)}")
        print(f"分组:    {', '.join(sub.groups) if sub.groups else '全部'}")
        print(f"关键词:  {', '.join(sub.keywords) if sub.keywords else '内置默认'}")
        print(f"webhook: python -m dots wechat hook --port 8790")
        return 0
    return 2


def db_cursor(cfg) -> str:
    """wechat 源的游标。单独开一条只读连接 —— 这条命令不该抢主连接。"""
    try:
        conn = db.connect(cfg.db)
    except Exception:
        return "(读不到库)"
    try:
        return db.cursor_get(conn, "wechat") or "0(还没读过)"
    finally:
        conn.close()


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
                              ("ical", cfg.sources.ical), ("wechat", cfg.sources.wechat))
               if s.enabled]
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

    if cfg.ontology.enabled:
        from dots import ontology as ont
        doc = ont.load(cfg)
        st = ont.status(cfg, doc)
        n = len(doc.get("entities") or [])
        check(
            "本体库", True,
            f"{n} 个实体 · {len(cfg.ontology.datasources)} 个数据源 · "
            + ("派生层最新" if st["synced"] else f"派生层待更新({st['reason']})"),
        )
        if not st["synced"]:
            print("      -> dots ontology apply")
        if cfg.ontology.datasources:
            missing = [ds.dsn_env for ds in cfg.ontology.datasources.values()
                       if ds.dsn_env and not os.environ.get(ds.dsn_env)]
            if missing:
                print("  ! 数据源凭据  缺环境变量:" + ", ".join(missing))
    else:
        print("  ! 本体库  已关闭([ontology] enabled = false)")

    if cfg.mcp.enabled or cfg.mcp.servers:
        from dots import mcp as mcp_mod
        ok, detail = mcp_mod.check(cfg)
        check("MCP", ok, detail,
              "dots mcp add jira|feishu|gitlab|sigma;缺依赖 pip install 'dots-agent[mcp]'")
    else:
        print("  ! MCP  没接外部系统(飞书/Jira/Sigma/GitLab)")

    if cfg.sdlc.enabled:
        from dots import sdlc as sdlc_mod
        pipelines = sdlc_mod.list_pipelines(cfg)
        active = [p for p in pipelines if p.get("status") == "active"]
        check("流水线", True, f"{len(pipelines)} 条,进行中 {len(active)} 条")
    else:
        print("  ! 流水线  已关闭([sdlc] enabled = false)")

    if cfg.sources.wechat.enabled:
        from dots import wechat as wechat_mod
        p = wechat_mod.spool_path(cfg)
        if p.exists():
            check("微信群 spool", True, f"{p}({p.stat().st_size} 字节)")
        else:
            check("微信群 spool", False, f"{p} 还不存在",
                  "跑一次 dots wechat status,或者让桥先写一条")

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

    from dots import bugs as bugs_mod
    from dots import mcp as mcp_mod
    from dots import ontology as ont_mod
    from dots import sdlc as sdlc_mod
    from dots import wechat as wechat_mod

    print("== 本体库 ==")
    ont_mod.demo()
    print("== 缺陷指纹 ==")
    bugs_mod.demo()
    print("== 微信群 spool ==")
    wechat_mod.demo()
    print("== MCP ==")
    mcp_mod.demo()
    print("== 流水线 ==")
    sdlc_mod.demo()

    try:
        from tests import (test_bridge, test_bugs, test_cli, test_loop, test_mcp,
                           test_ontology, test_sdlc, test_sources, test_wechat,
                           test_web)
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
    print("== 本体库(采集/手改/派生)==")
    print(test_ontology.run())
    print("== 缺陷 + 微信群 ==")
    print(test_bugs.run())
    print(test_wechat.run())
    print("== MCP 接入 ==")
    print(test_mcp.run())
    print("== 流水线 ==")
    print(test_sdlc.run())
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
