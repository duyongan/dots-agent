"""MCP:把飞书 / Jira / Sigma / GitLab 的工具挂到 agent 手上。

三条约定:

1. 配置在 `dot.toml` 的 `[mcp.servers.<名>]`,**密钥只写环境变量名**。
   参数和环境变量值里可以写 `${VAR}`,连的时候才展开 —— 这样 dot.toml
   里永远不出现明文口令,而 `npx @larksuiteoapi/lark-mcp -a ${FEISHU_APP_ID}`
   这种「必须把密钥放命令行」的服务也接得进来。
2. 工具名统一前缀 `<server>__<tool>`(`jira__create_issue`)。两个系统都有
   `create_issue` 时不撞名,审批规则也能按系统写通配(`jira__*` = ask)。
3. 没装 `langchain-mcp-adapters` 不报错、不拖垮 tick —— MCP 是增强,不是命脉。
   缺依赖这件事只在 `dots doctor` / `dots mcp list` 里说出来。

真连 server 用官方适配器;这里只负责:配置 -> 连接参数 -> 工具加前缀 -> 过闸门。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 现成的四家。`dots mcp add jira` 把它们写成 dot.toml 里的一段。
# 命令/参数是各家 MCP server 的官方或社区通行写法;用户随时可以手改。
PRESETS: dict[str, dict] = {
    "feishu": {
        "title": "飞书 / Lark",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@larksuiteoapi/lark-mcp", "mcp",
                 "-a", "${FEISHU_APP_ID}", "-s", "${FEISHU_APP_SECRET}"],
        "pass_env": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        "note": "发消息、读群、查文档、建多维表格。要 Node.js 和飞书自建应用的 app_id/app_secret。",
    },
    "jira": {
        "title": "Jira",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-atlassian"],
        "pass_env": ["JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN",
                     "JIRA_PERSONAL_TOKEN", "JIRA_PROJECTS_FILTER",
                     "JIRA_SSL_VERIFY", "CONFLUENCE_URL"],
        "note": "建单、查单、改状态、写评论。要 uv(uvx)和 Jira API token。",
    },
    "gitlab": {
        "title": "GitLab",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gitlab"],
        "pass_env": ["GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_URL"],
        "note": "建分支、提 MR、读流水线。要 Node.js 和一个有 api 权限的 access token。",
    },
    "sigma": {
        "title": "Sigma",
        "transport": "streamable_http",
        "command": "",
        "args": [],
        "url": "${SIGMA_MCP_URL}",
        "pass_env": ["SIGMA_MCP_URL", "SIGMA_API_TOKEN"],
        "note": "Sigma 一般用 HTTP 接入:把 SIGMA_MCP_URL 指向公司网关;"
                "如果你们是 stdio 版,改 command/args 即可。",
    },
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(value: str) -> str:
    """把 `${VAR}` 换成环境变量的值。没定义的留空,让 server 自己报错。"""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), str(value or ""))


def server_name(cfg, name: str) -> str:
    """工具前缀。只用小写字母数字和连字符 —— 有的模型对工具名字符很挑。"""
    server = cfg.mcp.servers.get(name)
    prefix = (server.prefix if server else "") or name
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix)
    return safe or "mcp"


def connection(server) -> dict:
    """配置里的一段 -> 适配器要的连接参数。"""
    transport = (server.transport or "stdio").strip().lower()
    if transport in ("sse", "http", "streamable_http", "streamable-http"):
        transport = "streamable_http" if "http" in transport or transport == "http" else "sse"
        return {"transport": transport, "url": expand(server.url)}
    env = {k: expand(v) for k, v in (server.env or {}).items()}
    for name in server.pass_env or []:
        value = os.environ.get(name, "")
        if value:
            env[name] = value
    conn: dict = {
        "transport": "stdio",
        "command": expand(server.command),
        "args": [expand(a) for a in (server.args or [])],
    }
    if env:
        conn["env"] = env
    return conn


def connections(cfg) -> dict[str, dict]:
    """所有启用的 server -> {名字: 连接参数}。配置不全的直接跳过并留一句原因。"""
    out: dict[str, dict] = {}
    for name, server in (cfg.mcp.servers or {}).items():
        if not server.enabled:
            continue
        conn = connection(server)
        if conn["transport"] in ("sse", "streamable_http"):
            if not conn.get("url"):
                continue
        elif not conn.get("command"):
            continue
        out[name] = conn
    return out


def describe(cfg) -> str:
    """给系统提示用的一句话:接上了哪些外部系统。"""
    if not cfg.mcp.enabled:
        return ""
    names = list(connections(cfg))
    if not names:
        return ""
    titles = []
    for name in names:
        preset = PRESETS.get(name)
        titles.append(preset["title"] if preset else name)
    return "已接外部系统(工具名带前缀):" + "、".join(
        f"{t}(`{server_name(cfg, n)}__*`)" for t, n in zip(titles, names)
    )


# --------------------------------------------------------------------- 连接

def adapter():
    """延迟导入 —— 没装这个包的人不该在 import dots.mcp 时就崩。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient


# 连一次很贵(要起 npx/uvx 子进程),同一个进程里复用。
# 键是连接参数本身,改了配置自然就不再命中。
_CACHE: dict[str, list] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(conns: dict[str, dict]) -> str:
    return json.dumps(conns, sort_keys=True, ensure_ascii=False)


async def _fetch(conns: dict[str, dict], timeout: float) -> list[tuple[str, object]]:
    client = adapter()(conns)
    out: list[tuple[str, object]] = []
    for name in conns:
        try:
            tools = await asyncio.wait_for(client.get_tools(server_name=name), timeout)
        except TypeError:          # 老版本没有 server_name 参数
            tools = await asyncio.wait_for(client.get_tools(), timeout)
        except Exception:
            continue               # 一个 server 挂了不影响其它
        for tool in tools or []:
            out.append((name, tool))
    return out


def _run_async(coro):
    """在同步上下文里跑一个协程。已经在事件循环里(桌面 bridge 的异步入口)时
    另起一条线程,别在别人的 loop 上嵌套 asyncio.run。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:      # noqa: BLE001 - 原样带回调用线程
            box["error"] = e

    t = threading.Thread(target=runner, name="dots-mcp")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def load_tools(cfg, *, quiet: bool = False) -> tuple[list, list[str]]:
    """连所有 MCP server,拿回加了前缀的工具。返回 (工具, 说明/告警)。

    任何失败都只变成一句说明 —— tick 不能因为 Jira 网关了就跑不动。
    """
    notes: list[str] = []
    if not getattr(cfg, "mcp", None) or not cfg.mcp.enabled:
        return [], notes
    conns = connections(cfg)
    if not conns:
        notes.append("MCP 开了,但没有一个 server 的配置是完整的")
        return [], notes

    key = _cache_key(conns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return list(cached), notes

    timeout = max((s.timeout for s in cfg.mcp.servers.values() if s.enabled), default=60.0)
    try:
        pairs = _run_async(_fetch(conns, timeout))
    except ImportError:
        notes.append(
            "没装 langchain-mcp-adapters,MCP 工具全部不可用:"
            "pip install 'dots-agent[mcp]'"
        )
        return [], notes
    except Exception as e:
        notes.append(f"连 MCP server 失败:{type(e).__name__}: {e}")
        return [], notes

    tools = []
    by_server: dict[str, int] = {}
    for name, tool in pairs:
        renamed = rename_tool(tool, server_name(cfg, name))
        if renamed is None:
            continue
        tools.append(renamed)
        by_server[name] = by_server.get(name, 0) + 1

    for name in conns:
        count = by_server.get(name, 0)
        if count == 0:
            notes.append(f"{name} 没拿到任何工具(命令/地址/凭据检查一下)")
        elif not quiet:
            notes.append(f"{name}: {count} 个工具")

    with _CACHE_LOCK:
        _CACHE[key] = list(tools)
    return tools, notes


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------- 工具包装

def rename_tool(tool, prefix: str):
    """给一个 langchain 工具换名字。换不了就返回 None(不拖垮别的)。

    args_schema 必须原样带过去 —— 丢了它,模型看到的参数说明就没了,
    调用必然出错。所以直接构造 StructuredTool,而不是让框架从函数签名去猜。
    """
    from langchain_core.tools import StructuredTool

    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None
    try:
        return StructuredTool(
            name=f"{prefix}__{tool.name}",
            description=tool.description or tool.name,
            args_schema=schema,
            func=_sync_caller(tool),
        )
    except Exception:
        return None


def _sync_caller(tool):
    """把一个可能只有 async 实现的工具包成同步函数。

    MCP 工具大多是 async-only。agent 用同步 .invoke 跑,拿到 async-only 工具
    会直接抛「Cannot invoke async tool」。这里统一在同步侧把它跑起来。
    """
    coroutine = getattr(tool, "coroutine", None)
    has_func = getattr(tool, "func", None) is not None

    def call(**kwargs):
        if has_func:
            return tool.invoke(kwargs)
        if coroutine is not None:
            return _run_async(coroutine(**kwargs))
        return tool.invoke(kwargs)

    return call


def gated(tools, gate):
    """给每个 MCP 工具套一层审批闸门。

    gate(action, args) -> None 放行 / 字符串 拒绝。动作名就是带前缀的工具名,
    所以 `[approval.rules]` 里可以写 `jira__* = "ask"`、`gitlab__create_* = "deny"`。
    闸门在调用之前跑 —— 拒了就没有任何外部请求发出去。
    """
    from langchain_core.tools import StructuredTool

    out = []
    for tool in tools:
        schema = getattr(tool, "args_schema", None)
        if schema is None:
            out.append(tool)
            continue
        inner = _sync_caller(tool)

        def guarded(_inner=inner, _tool=tool, **kwargs):
            blocked = gate(_tool.name, kwargs)
            if blocked:
                return blocked
            return _inner(**kwargs)

        try:
            out.append(StructuredTool(
                name=tool.name,
                description=tool.description or tool.name,
                args_schema=schema,
                func=guarded,
            ))
        except Exception:
            out.append(tool)
    return out


# --------------------------------------------------------------------- 配置写入

def add_preset(path, name: str, *, force: bool = False) -> Path:
    """把一家预设写进 dot.toml。已经有同名 server 时不覆盖,除非 force。"""
    preset = PRESETS.get(name)
    if preset is None:
        raise SystemExit(f"没有 {name!r} 这个预设。可用的:{', '.join(PRESETS)}")
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    # 只认真正的段头。模板里有一堆 `# [mcp.servers.jira]` 注释示例,
    # 用 in 判断会误以为已经配过了。
    header = re.compile(rf"(?m)^\s*\[mcp\.servers\.{re.escape(name)}\]\s*$")
    if header.search(text):
        if not force:
            raise SystemExit(f"{p} 里已经有 [mcp.servers.{name}],没动它(要覆盖加 --force)。")
        text = _drop_section(text, f"mcp.servers.{name}")

    lines = [f"[mcp.servers.{name}]", f"transport = {json.dumps(preset['transport'])}"]
    if preset.get("command"):
        lines.append(f"command = {json.dumps(preset['command'])}")
    if preset.get("args"):
        lines.append("args = " + json.dumps(list(preset["args"]), ensure_ascii=False))
    if preset.get("url"):
        lines.append(f"url = {json.dumps(preset['url'])}")
    if preset.get("pass_env"):
        lines.append("pass_env = " + json.dumps(list(preset["pass_env"])))
    for key, value in (preset.get("env") or {}).items():
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    block = "\n".join(lines) + "\n"

    if "[mcp.servers." in text:
        new = text.rstrip("\n") + "\n\n" + block
    else:
        new = (text.rstrip("\n") + "\n\n" + block) if text.strip() else block
    # 写了 server 就得把开关打开,不然配置齐了也不会连。
    new = _enable_mcp_section(new)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    tomllib.loads(p.read_text(encoding="utf-8"))     # 语法坏了立刻炸,别留个坏文件
    return p


def _drop_section(text: str, section: str) -> str:
    """删掉一个 [section] 段(到下一个段头为止)。--force 覆盖时用。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section}]":
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def _enable_mcp_section(text: str) -> str:
    """把 [mcp] 段里的 enabled = false 改成 true;没有这段就补一段。

    不用正则一次匹配:模板里 [mcp] 和 enabled 之间夹着注释,`[mcp]\\nenabled`
    这种写法根本匹配不上(踩过)。
    """
    lines = text.splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[mcp]":
            inside = True
            continue
        if inside and stripped.startswith("["):
            break
        if inside and re.match(r"^(\s*)enabled\s*=\s*false\s*$", line):
            indent = re.match(r"^(\s*)", line).group(1)
            nl = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"{indent}enabled = true{nl}"
            return "".join(lines)
    if any(line.strip() == "[mcp]" for line in lines):
        return "".join(lines)          # 有段但没有 enabled = false,不动
    return "[mcp]\nenabled = true\n\n" + "".join(lines)


def check(cfg) -> tuple[bool, str]:
    """doctor 用:装没装适配器、配了几个 server、缺哪些环境变量。"""
    if not cfg.mcp.enabled:
        return True, "没开([mcp] enabled = false)"
    conns = connections(cfg)
    if not conns:
        return False, "开了但没有任何完整配置的 server"
    try:
        adapter()
    except ImportError:
        return False, f"配了 {len(conns)} 个 server,但没装 langchain-mcp-adapters"
    missing = []
    for name, server in cfg.mcp.servers.items():
        if not server.enabled:
            continue
        for env_name in server.pass_env or []:
            if not os.environ.get(env_name):
                missing.append(env_name)
        for value in [server.url, server.command, *(server.args or [])]:
            for ref in _ENV_REF.findall(str(value or "")):
                if not os.environ.get(ref):
                    missing.append(ref)
    if missing:
        return False, "缺环境变量:" + ", ".join(sorted(set(missing)))
    return True, f"{len(conns)} 个 server:" + ", ".join(sorted(conns))


def demo() -> None:
    """不连真 server 的自检:展开、前缀、闸门、配置写入。"""
    os.environ.setdefault("DOTS_MCP_DEMO_TOKEN", "tok")
    assert expand("${DOTS_MCP_DEMO_TOKEN}") == "tok"
    assert expand("${NOT_DEFINED_ANYWHERE}") == ""

    server = type("S", (), {
        "enabled": True, "transport": "stdio", "command": "uvx",
        "args": ["mcp-atlassian"], "url": "",
        "env": {"JIRA_PROJECTS_FILTER": "OPS"},
        "pass_env": ["DOTS_MCP_DEMO_TOKEN"], "timeout": 5.0, "prefix": "jira",
    })()
    conn = connection(server)
    assert conn["command"] == "uvx" and conn["args"] == ["mcp-atlassian"]
    assert conn["env"]["JIRA_PROJECTS_FILTER"] == "OPS"
    assert conn["env"]["DOTS_MCP_DEMO_TOKEN"] == "tok"

    http_server = type("S", (), {
        "enabled": True, "transport": "http", "command": "", "args": [],
        "url": "https://sigma.example/mcp", "env": {}, "pass_env": [],
        "timeout": 5.0, "prefix": "",
    })()
    assert connection(http_server) == {
        "transport": "streamable_http", "url": "https://sigma.example/mcp"}

    cfg = type("C", (), {"mcp": type("M", (), {
        "enabled": True,
        "servers": {"jira": server, "sigma": http_server},
    })()})()
    assert server_name(cfg, "jira") == "jira"
    assert "jira" in describe(cfg) and "Sigma" in describe(cfg)

    # 闸门:拒绝时不调用内层
    from pydantic import BaseModel

    class _Args(BaseModel):
        summary: str

    called = []

    class _FakeTool:
        name = "jira__create_issue"
        description = "create"
        args_schema = _Args
        coroutine = None

        @staticmethod
        def func(**kwargs):
            called.append(kwargs)
            return "created"

        def invoke(self, kwargs):
            return self.func(**kwargs)

    fake = _FakeTool()
    blocked = gated([fake], lambda action, args: "被规则拒绝" if action == "jira__create_issue" else None)
    assert blocked[0].invoke({"summary": "x"}) == "被规则拒绝"
    assert called == []
    allowed = gated([fake], lambda action, args: None)
    assert allowed[0].invoke({"summary": "x"}) == "created"
    assert called == [{"summary": "x"}]
    print("mcp: ok")


if __name__ == "__main__":
    demo()
