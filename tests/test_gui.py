"""图形界面的 API、鉴权和聊天中断。不联网。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from dots import __version__, db, graph, gui, worker
from dots import config as config_mod


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSaver:
    def __init__(self):
        self.conn = _FakeConn()


class _FakeAgent:
    def __init__(self):
        self.messages = []
        self.invokes = []

    def get_state(self, config):
        return {"messages": list(self.messages)}

    def invoke(self, payload, config):
        self.invokes.append(payload)
        if isinstance(payload, Command):
            self.messages.append(AIMessage(content="批准后完成"))
            return {"messages": list(self.messages)}
        self.messages.append(AIMessage(content=""))
        return {"__interrupt__": [SimpleNamespace(
            value={"action": "send_email", "args": {"to": "a@b"}}
        )]}


class _FakeSession:
    def __init__(self, cfg, agent):
        self.conn = db.connect(cfg.db)
        db.init(self.conn)
        self.agents = {"dot": agent}
        self.ctx = graph.RunCtx(cfg=cfg, conn=self.conn)
        self.saver = _FakeSaver()
        self.pending = None

    def close(self):
        self.conn.close()
        self.saver.conn.close()


def _make_service():
    d = Path(tempfile.mkdtemp(prefix="dots-gui-test-"))
    path = d / "dot.toml"
    config_mod.ensure_file(path)
    svc = gui.Service(path)
    svc.cfg = config_mod.load(path)
    svc.conn = db.connect(svc.cfg.db)
    db.init(svc.conn)
    svc.agents = {"dot": object()}
    svc.ctx = graph.RunCtx(cfg=svc.cfg, conn=svc.conn)
    return svc


def _start(svc):
    srv = gui.make_server(svc, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{svc.port}"


def _req(base, path, method="GET", body=None, token=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Dots-Token"] = token
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def test_health_and_token_required() -> None:
    svc = _make_service()
    srv, base = _start(svc)
    try:
        status, health = _req(base, "/api/health")
        assert status == 200 and health["version"] == __version__

        status, err = _req(base, "/api/skills", "POST",
                           {"name": "x", "description": "测试。", "body": "b"})
        assert status == 403 and err["error"]["code"] == "forbidden"

        status, ok = _req(base, "/api/skills", "POST",
                          {"name": "x", "description": "测试。", "body": "b"},
                          token=svc.token)
        assert status == 201 and ok["name"] == "x"
    finally:
        srv.shutdown()
        srv.server_close()
        svc.shutdown()


def test_static_index_injects_token() -> None:
    if not (gui.WEBUI / "index.html").exists():
        return
    svc = _make_service()
    srv, base = _start(svc)
    try:
        with urllib.request.urlopen(base + "/") as r:
            html = r.read().decode("utf-8")
        assert svc.token in html
        assert "%DOTS_TOKEN%" not in html
    finally:
        srv.shutdown()
        srv.server_close()
        svc.shutdown()


def test_config_save() -> None:
    svc = _make_service()
    srv, base = _start(svc)
    try:
        status, err = _req(base, "/api/config", "PUT", {
            "approval": {"rules": {"send_email": "bogus"}},
        }, token=svc.token)
        assert status == 400 and err["error"]["code"] == "validation"

        status, res = _req(base, "/api/config", "PUT", {
            "model": {"name": "openai:deepseek-chat",
                      "base_url": "https://api.deepseek.com",
                      "api_key_env": "DEEPSEEK_API_KEY",
                      "max_tokens": 2048, "temperature": 0.1},
            "dot": {"tick_seconds": 30},
            "approval": {"rules": {"send_email": "deny"}},
            "secrets": {"DEEPSEEK_API_KEY": "sk-test"},
        }, token=svc.token)
        assert status == 200, res
        assert res["config"]["model"]["max_tokens"] == 2048
        assert res["secrets"]["set"]["DEEPSEEK_API_KEY"] is True
        text = svc.config_path.read_text(encoding="utf-8")
        assert "最该先接的源" in text          # 整行注释保留
        assert 'send_email = "deny"' in text
        assert (svc.config_path.parent / "secrets.env").exists()
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        srv.shutdown()
        srv.server_close()
        svc.shutdown()


def test_chat_interrupt_and_resume() -> None:
    svc = _make_service()
    agent = _FakeAgent()
    svc.chat = _FakeSession(svc.cfg, agent)
    prev_ctx = graph.current()
    srv, base = _start(svc)
    try:
        status, res = _req(base, "/api/chat", "POST", {"text": "帮我发邮件"},
                           token=svc.token)
        assert status == 200, res
        assert res["interrupt"]["action"] == "send_email"
        assert res["reply"] is None

        status, err = _req(base, "/api/chat", "POST", {"text": "再发一句"},
                           token=svc.token)
        assert status == 409 and err["error"]["code"] == "pending"

        status, res = _req(base, "/api/chat/resume", "POST", {"approved": True},
                           token=svc.token)
        assert status == 200, res
        assert res["interrupt"] is None
        assert res["reply"] == "批准后完成"
        assert isinstance(agent.invokes[-1], Command)

        assert graph.current() is prev_ctx           # 用完复位
        assert svc.chat.ctx.chat is False
    finally:
        srv.shutdown()
        srv.server_close()
        svc.shutdown()


def test_skills_crud() -> None:
    svc = _make_service()
    srv, base = _start(svc)
    try:
        status, _ = _req(base, "/api/skills", "POST",
                         {"name": "write-report", "description": "写周报。",
                          "body": "# 周报"}, token=svc.token)
        assert status == 201

        status, one = _req(base, "/api/skills/write-report")
        assert status == 200 and one["description"] == "写周报。"

        status, err = _req(base, "/api/skills", "POST",
                           {"name": "write-report", "description": "重复。",
                            "body": ""}, token=svc.token)
        assert status == 409

        status, _ = _req(base, "/api/skills/write-report", "PUT",
                         {"description": "写一份周报。", "body": "# 新版"},
                         token=svc.token)
        assert status == 200

        status, _ = _req(base, "/api/skills/write-report", "DELETE", token=svc.token)
        assert status == 200
        status, err = _req(base, "/api/skills/write-report")
        assert status == 404
    finally:
        srv.shutdown()
        srv.server_close()
        svc.shutdown()


def test_path_traversal_rejected() -> None:
    svc = _make_service()
    srv, base = _start(svc)
    try:
        status, err = _req(base, "/api/skills/..%2F..%2Fevil", "DELETE", token=svc.token)
        assert status == 400
        assert err["error"]["code"] == "validation"
    finally:
        srv.shutdown()
        srv.server_close()
        svc.shutdown()
