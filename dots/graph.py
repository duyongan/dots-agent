"""create_deep_agent 的组装,以及 agent 能调的动作工具。

两个闸门,别搞混:
  rules.decide()   规则层。ALLOW 直接过,ASK 走 interrupt,DENY 直接拒。
  interrupt()      langgraph 原语。第一次调用会中断整张图,把控制权交出来。
                   续跑时它「返回」你传进去的 resume 值。
所以 interrupt 之后的代码只在人批了之后才执行 —— 这是整套审批的地基。

每个 bot 一张编译好的图。共用同一个 checkpointer,所以交接后
新 bot 用同一个 thread_id 就能看到前任的全部消息 —— 交接是同一条
对话线上换个人接手,不是另起一条。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import SubAgent
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.types import interrupt

from dots import approvals, db
from dots import rules as rules_mod
from dots import skills as skills_mod
from dots.prompts import SCOUT_PROMPT, SYSTEM_PROMPT

SHELL_TIMEOUT = 120

# 这些工具不受 bot 的 tools 白名单限制 —— 它们是生命周期工具,
# 不是能力工具。少了 complete_goal,目标就永远完不成。
CORE_TOOLS = {"complete_goal", "create_goal", "handoff_to", "save_skill"}


@dataclass
class RunCtx:
    """当前这一轮在跑哪个 goal、哪个 bot。

    单机单 worker,一次只跑一个 goal,所以一个模块级实例够用。
    ponytail: 全局单值;哪天要并发跑多个 goal,改成从 config 里取。
    """
    cfg: object
    conn: object
    goal_id: str | None = None
    bot: str = "dot"


CURRENT: RunCtx | None = None


# --------------------------------------------------------------------- 闸门

def _gate(action: str, args: dict) -> str | None:
    """放行返回 None;拦住返回一句给模型看的话。"""
    ctx = CURRENT
    verdict = rules_mod.decide(action, ctx.cfg.rules)
    db.audit(ctx.conn, ctx.goal_id, "gate", f"{action} -> {verdict}")

    if verdict == rules_mod.DENY:
        return (
            f"被规则拒绝:{action} 不在白名单内。不要重试,也不要绕路。"
            f"把你原本想做的事写进最终结果里,让人来决定。"
        )
    if verdict == rules_mod.ASK:
        answer = interrupt({"action": action, "args": args, "goal_id": ctx.goal_id})
        approved = bool(answer.get("approved")) if isinstance(answer, dict) else bool(answer)
        if not approved:
            db.audit(ctx.conn, ctx.goal_id, "rejected_by_human", action)
            return f"被人拒绝了:{action}。不要重试。把这件事写进最终结果里。"
        db.audit(ctx.conn, ctx.goal_id, "approved_by_human", action)
    return None


# --------------------------------------------------------------------- 工具

def make_tools(ctx: RunCtx, bot_cfg=None):
    """工具用闭包拿 ctx,所以每个 bot 的图只 build 一次。

    bot 的 tools 白名单只筛「能力工具」(发邮件、跑命令),
    生命周期工具(CORE_TOOLS)一律保留。
    """

    @tool
    def complete_goal(summary: str, evidence: str) -> str:
        """把这个目标标记为完成。

        必须在真正做完之后调用。evidence 是能让人核对的东西:工作区里的相对路径、
        邮件 id、URL。没有 evidence 的完成会进待复核队列,而不是 done。
        目标做不到也要调,把原因写在 summary 里。
        """
        has = bool((evidence or "").strip())
        db.set_goal(
            ctx.conn,
            ctx.goal_id,
            status=db.DONE if has else db.REVIEW,
            summary=summary,
            evidence=(evidence or "").strip(),
        )
        db.audit(
            ctx.conn, ctx.goal_id, "complete_goal",
            f"{summary[:160]} | evidence={evidence[:160] or '(无)'}",
        )
        return "已标记完成" if has else "已提交,但因缺少 evidence 进入待复核"

    @tool
    def create_goal(text: str, reason: str) -> str:
        """发现一个「不先做它就完不成当前目标」的新任务时,记下来。

        新目标一律是 pending,要人工批准才会跑。所以别投机性地建,
        也别为了「以后可能有用」而建。
        """
        gid = db.add_goal(
            ctx.conn, text, origin=f"agent:{ctx.goal_id}",
            status=db.PENDING, parent_id=ctx.goal_id, bot=ctx.bot,
        )
        db.audit(ctx.conn, ctx.goal_id, "create_goal", f"{gid} {text[:140]} | {reason[:140]}")
        return f"已记为 {gid},待人工批准。"

    @tool
    def save_skill(name: str, description: str, body: str) -> str:
        """把你刚摸索出来的可复用流程存成技能。

        下次遇到同类任务,它会出现在你的技能索引里(本轮不会生效 ——
        技能是在开跑之前加载的)。

        name        小写连字符,要和目录名一致,最多 64 字符
        description 一句话,最多 60 字符,句号结尾。它每一轮都会进系统提示
                    并被截断,超出的部分永远不起作用。
        body        markdown 正文。写「步骤」,不写「这次任务的故事」。
                    建议结构:## When to Use / ## Steps / ## Pitfalls

        校验不过会被拒,拒绝理由会告诉你怎么改 —— 改完再存。
        """
        if not ctx.cfg.skills.enabled:
            return "技能功能被关掉了([skills] enabled = false)。"
        problem = skills_mod.validate(name, description)
        if problem:
            return f"没存成。{problem}"
        blocked = _gate("save_skill", {"name": name})
        if blocked:
            return blocked
        path = skills_mod.save(ctx.cfg, name, description, body)
        db.audit(ctx.conn, ctx.goal_id, "save_skill", f"{name}: {description}")
        return f"已存到 {path}。下轮开跑时才会进索引。"

    @tool
    def handoff_to(bot: str, reason: str, note: str) -> str:
        """把这个目标交给另一个 bot。交出去之后你这一轮就结束了。

        只在确实该换人时用:比如你是调研的,但下一步要对外发东西;
        或者这个目标整个就是写作。能自己做完就自己做完。

        note 是给接手方的简报:说清已经做了什么、材料在哪、下一步干什么。
        交接是有次数上限的,来回甩锅会被拦。
        """
        goal = db.get_goal(ctx.conn, ctx.goal_id)
        if goal is None:
            return "目标不存在了。"
        target = (bot or "").strip().lower()
        if target == goal["bot"]:
            return f"你现在就是 {target},不能交给自己。"
        if target not in ctx.cfg.bots:
            return (
                f"没有叫 {target!r} 的 bot。可用的:"
                f"{', '.join(sorted(ctx.cfg.bots))}"
            )
        hops = goal["handoffs"]
        if hops >= ctx.cfg.handoff.max_hops:
            return (
                f"这个目标已经交接 {hops} 次,到上限 {ctx.cfg.handoff.max_hops} 了。"
                f"没人接就自己做完;做不完就如实说卡在哪。不要再交出去。"
            )
        briefing = f"{reason.strip()}\n\n{note.strip()}".strip()
        if not db.handoff_goal(ctx.conn, ctx.goal_id, target, goal["bot"], briefing):
            return "交接失败:目标状态已经变了。"
        db.audit(
            ctx.conn, ctx.goal_id, "handoff",
            f"{goal['bot']} -> {target} (第 {hops + 1} 次): {reason[:120]}",
        )
        return f"已交给 {target}。你这一轮到此结束,不要再动这个目标。"

    @tool
    def send_email(to: str, subject: str, body: str) -> str:
        """给某人发邮件。可能被规则拒绝,也可能挂起等人工批准。被拒了不要重试。"""
        args = {"to": to, "subject": subject, "body": body}
        blocked = _gate("send_email", args)
        if blocked:
            return blocked
        try:
            approvals.send_mail(ctx.cfg, to, subject, body)
        except Exception as e:
            db.audit(ctx.conn, ctx.goal_id, "send_email_failed", f"{type(e).__name__}: {e}")
            return f"发送失败:{type(e).__name__}: {e}"
        db.audit(ctx.conn, ctx.goal_id, "send_email", f"-> {to}: {subject[:80]}")
        return f"已发送给 {to}"

    @tool
    def run_shell(cmd: str) -> str:
        """在工作区里执行一条命令。默认被规则拒绝,只有在 [approval.rules] 里放开才可用。

        ponytail: shell=True + 只靠 cwd 兜着,这不是沙箱。要真隔离就换容器 backend。
        """
        args = {"cmd": cmd}
        blocked = _gate("run_shell", args)
        if blocked:
            return blocked
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(ctx.cfg.workspace),
                capture_output=True, text=True, timeout=SHELL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"超时({SHELL_TIMEOUT}s)"
        out = (proc.stdout or "") + (proc.stderr or "")
        db.audit(ctx.conn, ctx.goal_id, "run_shell", f"{cmd[:100]} -> {proc.returncode}")
        return f"exit={proc.returncode}\n{out[:4000]}"

    tools = [complete_goal, create_goal, save_skill, handoff_to, send_email, run_shell]

    allowed = list(getattr(bot_cfg, "tools", []) or [])
    if allowed:
        tools = [t for t in tools if t.name in CORE_TOOLS or t.name in allowed]
    return tools


# --------------------------------------------------------------------- 组装

def build_model(cfg):
    kwargs: dict = {
        "temperature": cfg.model.temperature,
        "max_tokens": cfg.model.max_tokens,
    }
    if cfg.model.base_url:
        kwargs["base_url"] = cfg.model.base_url
    if cfg.model.api_key_env:
        key = os.environ.get(cfg.model.api_key_env, "")
        if not key:
            raise SystemExit(
                f"[model] api_key_env = {cfg.model.api_key_env!r} 但那个环境变量是空的。\n"
                f"设上它,或者改用 provider 自带的默认变量名。"
            )
        kwargs["api_key"] = key
    try:
        return init_chat_model(cfg.model.name, **kwargs)
    except Exception as e:
        raise SystemExit(
            f"建不出模型 {cfg.model.name!r}:{type(e).__name__}: {e}\n"
            f"检查 [model] 段。第三方端点(DeepSeek 等)要三件套:"
            f"name = \"openai:<模型>\",base_url,api_key_env。"
        )


def build(cfg, checkpointer, ctx: RunCtx, bot: str = "dot"):
    """为一个 bot 建一张图。checkpointer 是「崩了续跑」和「交接有上下文」的全部依靠。"""
    bot_cfg = cfg.bot(bot)
    prompt = SYSTEM_PROMPT
    if bot_cfg.prompt:
        prompt = f"{SYSTEM_PROMPT}\n---\n\n{bot_cfg.prompt}"

    kwargs: dict = {}
    if cfg.skills.enabled:
        # 路径相对于 backend 的 root,必须是 POSIX 风格。技能从磁盘实时读,
        # 所以 agent 自己写的新技能下次开跑就生效。
        kwargs["skills"] = [f"/{cfg.skills.dir}/"]

    return create_deep_agent(
        model=build_model(cfg),
        tools=make_tools(ctx, bot_cfg),
        system_prompt=prompt,
        subagents=[
            SubAgent(
                name="scout",
                description=(
                    "只读研究员。把一个「需要翻很多文件才能回答」的问题交给它,"
                    "它只回摘要,不把原文带回来。"
                ),
                system_prompt=SCOUT_PROMPT,
            )
        ],
        # 沙箱边界就是这个 root_dir —— 不是内核隔离,是路径约束。
        # virtual_mode=True 是必须的:默认 False 时绝对路径和 ".." 能绕出 root_dir,
        # 那这个「沙箱」就只是句好话。设了它,越界路径会被拒。
        backend=FilesystemBackend(root_dir=str(cfg.workspace), virtual_mode=True),
        checkpointer=checkpointer,
        name=f"dot-{bot}",
        **kwargs,
    )
