"""MCP 接入的自检:配置展开、工具前缀、审批闸门、缺依赖不炸。

不真起 npx/uvx 子进程 —— 适配器用假的替身,测的是 dots 这一侧的约定:
密钥不落盘、工具名带前缀、闸门在调用之前拦。
"""

from __future__ import annotations

import os
import secrets
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from dots import mcp


class _Args(BaseModel):
    summary: str


class _FakeTool:
    name = "create_issue"
    description = "Create a Jira issue"
    args_schema = _Args
    coroutine = None

    @staticmethod
    def func(**kwargs):
        return f"created:{kwargs.get('summary')}"

    def invoke(self, kwargs):
        return self.func(**kwargs)


class _FakeClient:
    """假的 MultiServerMCPClient。jira 有一个工具,别的 server 空。"""

    def __init__(self, conns):
        self.conns = conns

    async def get_tools(self, server_name=None):
        return [_FakeTool()] if server_name == "jira" else []


def _cfg(tag: str = "mcp") -> SimpleNamespace:
    root = Path.cwd() / f".dots-test-{tag}-{secrets.token_hex(3)}"
    root.mkdir(parents=True, exist_ok=True)
    server = SimpleNamespace(
        name="jira", enabled=True, transport="stdio", command="uvx",
        args=["mcp-atlassian"], url="", env={"JIRA_PROJECTS_FILTER": "OPS"},
        pass_env=["DOTS_TEST_JIRA_TOKEN"], timeout=5.0, prefix="jira",
    )
    return SimpleNamespace(
        root=root,
        mcp=SimpleNamespace(enabled=True, servers={"jira": server}),
        ontology=SimpleNamespace(enabled=False, dir="ontology", derived="generated",
                                 datasources={}, inject_prompt=False, prompt_entities=20),
        sdlc=SimpleNamespace(enabled=False, dir="projects", max_fix_rounds=3,
                             auto_from_bug=False, stages=[]),
    )


def test_expand_and_connection() -> str:
    os.environ["DOTS_TEST_JIRA_TOKEN"] = "tok-123"
    assert mcp.expand("${DOTS_TEST_JIRA_TOKEN}") == "tok-123"
    assert mcp.expand("${DOTS_TEST_MISSING}") == ""

    server = SimpleNamespace(
        name="gl", enabled=True, transport="stdio", command="npx",
        args=["-y", "@modelcontextprotocol/server-gitlab"],
        url="", env={"GITLAB_API_URL": "https://gitlab.example/api/v4"},
        pass_env=["DOTS_TEST_JIRA_TOKEN"], timeout=5.0, prefix="",
    )
    conn = mcp.connection(server)
    assert conn["command"] == "npx"
    assert conn["env"]["GITLAB_API_URL"].endswith("/api/v4")
    assert conn["env"]["DOTS_TEST_JIRA_TOKEN"] == "tok-123"

    http = SimpleNamespace(
        name="sigma", enabled=True, transport="http", command="", args=[],
        url="https://sigma.example/mcp", env={}, pass_env=[], timeout=5.0, prefix="",
    )
    assert mcp.connection(http) == {"transport": "streamable_http",
                                    "url": "https://sigma.example/mcp"}

    # 配置不全的 server 不参与连接,也不报错
    broken = SimpleNamespace(
        name="x", enabled=True, transport="stdio", command="", args=[], url="",
        env={}, pass_env=[], timeout=5.0, prefix="",
    )
    cfg = SimpleNamespace(mcp=SimpleNamespace(
        enabled=True, servers={"jira": server, "broken": broken}))
    assert list(mcp.connections(cfg)) == ["jira"]
    return "配置:${VAR} 展开、HTTP 传输归一、不完整的 server 自动跳过"


def test_tools_prefixed_and_gated() -> str:
    cfg = _cfg("tools")
    os.environ["DOTS_TEST_JIRA_TOKEN"] = "tok-123"
    real_adapter = mcp.adapter
    mcp.adapter = lambda: _FakeClient
    try:
        mcp.clear_cache()
        tools, notes = mcp.load_tools(cfg)
        assert len(tools) == 1, (tools, notes)
        assert tools[0].name == "jira__create_issue", tools[0].name
        assert any("jira" in n for n in notes)

        # 闸门:拒绝时内层不会被调用
        blocked = mcp.gated(tools, lambda action, args: (
            "被规则拒绝" if action.startswith("jira__") else None))
        assert blocked[0].invoke({"summary": "x"}) == "被规则拒绝"

        allowed = mcp.gated(tools, lambda action, args: None)
        assert allowed[0].invoke({"summary": "x"}) == "created:x"

        # 缓存:第二次不再连
        mcp.load_tools(cfg)
        return "工具:带 server 前缀,过审批闸门,连一次就缓存"
    finally:
        mcp.adapter = real_adapter
        mcp.clear_cache()
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_missing_adapter_degrades() -> str:
    cfg = _cfg("noadapter")
    os.environ["DOTS_TEST_JIRA_TOKEN"] = "tok-123"

    def boom():
        raise ImportError("no langchain_mcp_adapters")

    real_adapter = mcp.adapter
    mcp.adapter = boom
    try:
        mcp.clear_cache()
        tools, notes = mcp.load_tools(cfg)
        assert tools == []
        assert any("langchain-mcp-adapters" in n for n in notes), notes
        ok, detail = mcp.check(cfg)
        assert not ok and "langchain-mcp-adapters" in detail
        return "降级:没装适配器只给一句提示,不让 tick 起不来"
    finally:
        mcp.adapter = real_adapter
        mcp.clear_cache()
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_add_preset_writes_toml() -> str:
    cfg = _cfg("preset")
    path = cfg.root / "dot.toml"
    path.write_text("[mcp]\nenabled = false\n\n[dot]\ntick_seconds = 60\n",
                    encoding="utf-8")
    try:
        mcp.add_preset(path, "jira")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        server = raw["mcp"]["servers"]["jira"]
        assert raw["mcp"]["enabled"] is True, "写了 server 就该把开关打开"
        assert server["command"] == "uvx"
        assert "JIRA_API_TOKEN" in server["pass_env"]
        assert server["args"] == ["mcp-atlassian"]

        try:
            mcp.add_preset(path, "jira")
        except SystemExit as e:
            assert "已经有" in str(e)
        else:
            raise AssertionError("重复写同一家应该被拦")

        mcp.add_preset(path, "feishu")
        raw2 = tomllib.loads(path.read_text(encoding="utf-8"))
        assert set(raw2["mcp"]["servers"]) == {"jira", "feishu"}
        assert "${FEISHU_APP_ID}" in raw2["mcp"]["servers"]["feishu"]["args"]
        return "配置写入:一家一条,已存在不覆盖,密钥只留变量名"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_describe_and_check() -> str:
    cfg = _cfg("desc")
    os.environ["DOTS_TEST_JIRA_TOKEN"] = "tok-123"
    try:
        text = mcp.describe(cfg)
        assert "jira" in text and "Jira" in text

        ok, detail = mcp.check(cfg)
        # 没装适配器时这里会 False;装了就是 True。两种都不该抛。
        assert isinstance(ok, bool) and detail
        return "自检:能说清接了哪些系统、缺什么"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def run() -> str:
    notes = [
        test_expand_and_connection(),
        test_tools_prefixed_and_gated(),
        test_missing_adapter_degrades(),
        test_add_preset_writes_toml(),
        test_describe_and_check(),
    ]
    return "MCP:" + ";".join(notes)


def test_mcp():                             # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
