"""微信群监控的自检:spool 增量读、half-line、webhook、BUG 去重派单。

不登录微信,也不跑真桥 —— 测的是 dots 这一侧:契约稳不稳、半行会不会丢、
十个人报同一条错会不会变成十个目标。
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dots import db, sources, wechat, worker


def _cfg(tag: str = "wx") -> SimpleNamespace:
    root = Path.cwd() / f".dots-test-{tag}-{secrets.token_hex(3)}"
    root.mkdir(parents=True, exist_ok=True)
    cfg = SimpleNamespace(
        root=root,
        workspace=root / "workspace",
        db=root / "dot.db",
        sources=SimpleNamespace(
            wechat=SimpleNamespace(
                enabled=True, backend="spool", library_path="",
                python_exe="", collect_timeout=240,
                collect_interval=300, max_pull=20,
                spool="wechat/inbox.jsonl", groups=[], keywords=[],
                interval=60, max_batch=100, token_env="X", token="",
            ),
        ),
        sdlc=SimpleNamespace(enabled=False, dir="projects", max_fix_rounds=3,
                             auto_from_bug=False, stages=[]),
        ontology=SimpleNamespace(enabled=False, dir="ontology", derived="generated",
                                 datasources={}, inject_prompt=True, prompt_entities=20),
        mcp=SimpleNamespace(enabled=False, servers={}),
    )
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


def test_spool_incremental() -> str:
    cfg = _cfg("spool")
    try:
        wechat.append(cfg, group="项目A", sender="张三", text="导出报 500")
        wechat.append(cfg, group="项目A", sender="李四", text="收到")
        msgs, cursor = wechat.read_new(cfg, "")
        assert len(msgs) == 2 and cursor
        assert wechat.read_new(cfg, cursor)[0] == []

        # 半行留到下次
        p = wechat.spool_path(cfg)
        with p.open("ab") as f:
            f.write('{"group":"A","text":"还没写完'.encode("utf-8"))
        assert wechat.read_new(cfg, cursor)[0] == []
        with p.open("ab") as f:
            f.write('"}\n'.encode("utf-8"))
        rest, _ = wechat.read_new(cfg, cursor)
        assert len(rest) == 1 and rest[0].text == "还没写完"
        return "spool:增量读不重复,半行不会被吃掉"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_poll_only_bugs_and_groups() -> str:
    cfg = _cfg("poll")
    try:
        wechat.append(cfg, group="项目A", sender="张三", text="订单导出报 500")
        wechat.append(cfg, group="项目A", sender="李四", text="中午吃什么")
        wechat.append(cfg, group="闲聊群", sender="王五", text="线上挂了")

        events, cursor = sources.wechat_poll(cfg, None, "")
        assert len(events) == 2, [e.payload["text"] for e in events]
        assert all(e.source == "wechat" for e in events)
        assert events[0].key.startswith("sha1:")

        cfg.sources.wechat.groups = ["项目A"]
        events2, _ = sources.wechat_poll(cfg, None, "")
        assert len(events2) == 1 and events2[0].payload["group"] == "项目A"
        return "过滤:只放行 BUG 消息,分组白名单生效"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_webhook() -> str:
    cfg = _cfg("hook")
    cfg.sources.wechat.token = "s3cret"
    httpd = wechat.serve(cfg, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def post(payload, token="s3cret"):
            last = None
            for attempt in range(3):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/wechat",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Dots-Token": token},
                    method="POST",
                )
                try:
                    return urllib.request.urlopen(req, timeout=5)
                except (urllib.error.URLError, ConnectionError) as e:
                    last = e          # 服务线程刚起来时的偶发拒连,重试一次
                    time.sleep(0.1 * (attempt + 1))
            raise last

        resp = post({"group": "项目A", "sender": "张三", "text": "接口 502 了"})
        assert json.loads(resp.read().decode())["written"] == 1

        try:
            post({"group": "A", "text": "x"}, token="wrong")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        else:
            raise AssertionError("错 token 应该 401")

        health = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5).read().decode())
        assert health["ok"]

        msgs, _ = wechat.read_new(cfg, "")
        assert len(msgs) == 1 and "502" in msgs[0].text
        return "webhook:推消息进 spool,token 校验挡住了错来源"
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_route_dedupes_into_one_goal() -> str:
    cfg = _cfg("route")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            def event(text, key):
                row_id = db.add_event(conn, "wechat", key, {
                    "group": "项目A", "sender": "张三", "text": text,
                })
                assert row_id
                return db.new_events(conn)[0]

            stats = {"bugs_new": 0, "bugs_repeat": 0, "goals_new": 0}
            worker._route_wechat(cfg, conn, event("订单导出报 500", "m1"), {
                "group": "项目A", "sender": "张三", "text": "订单导出报 500",
            }, stats)
            assert stats["bugs_new"] == 1 and stats["goals_new"] == 1

            # 第二条:同一个根因,只加计数,不再开目标
            worker._route_wechat(cfg, conn, event("又报 500 了,订单导出", "m2"), {
                "group": "项目A", "sender": "李四", "text": "又报 500 了,订单导出",
            }, stats)
            assert stats["bugs_new"] == 1 and stats["bugs_repeat"] == 1
            assert stats["goals_new"] == 1
            assert len(db.list_goals(conn)) == 1
            assert len(db.list_bugs(conn)) == 1
            assert db.list_bugs(conn)[0]["count"] == 2

            # 不像 BUG 的消息连台账都不进
            worker._route_wechat(cfg, conn, event("中午吃什么", "m3"), {
                "group": "项目A", "sender": "李四", "text": "中午吃什么",
            }, stats)
            assert len(db.list_bugs(conn)) == 1
        finally:
            conn.close()
        return "派单:同一条错只开一个目标,闲聊不进台账"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_auto_pipeline() -> str:
    """开着流水线时,BUG 应该直接起一条 bugfix 流水线,而不是普通目标。"""
    cfg = _cfg("autopipe")
    cfg.sdlc = SimpleNamespace(enabled=True, dir="projects", max_fix_rounds=3,
                               auto_from_bug=True, stages=[])
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            db.add_event(conn, "wechat", "m1", {})
            row = db.new_events(conn)[0]
            stats = {"bugs_new": 0, "bugs_repeat": 0, "goals_new": 0}
            worker._route_wechat(cfg, conn, row, {
                "group": "项目A", "sender": "张三", "text": "线上支付挂了 500",
            }, stats)
            assert stats["bugs_new"] == 1 and stats["goals_new"] == 0

            from dots import sdlc as sdlc_mod
            pipes = sdlc_mod.list_pipelines(cfg)
            assert len(pipes) == 1 and pipes[0]["track"] == "bugfix"
            assert pipes[0]["stage"] == "fix"

            bug = db.list_bugs(conn)[0]
            assert bug["pipeline_id"] == pipes[0]["id"]
            assert bug["status"] == db.BUG_FIXING
            # 流水线自己会派第一个目标
            goals = db.list_goals(conn)
            assert len(goals) == 1 and "【SDLC" in goals[0]["text"]
        finally:
            conn.close()
        return "自动化:群里的 BUG 直接进修复流水线,并绑上台账"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def _fake_pyweixin(running: bool = True):
    """假的 pyweixin 包:形状照着真库(静态类 + 中文键),不碰 UI。"""
    import types

    class Tools:
        @staticmethod
        def is_weixin_running():
            return running

        @staticmethod
        def get_weixin_hwnd():
            return 123 if running else 0

    class Messages:
        calls: list[tuple[str, int]] = []
        records = [
            {"消息发送人": "张三", "消息内容": "订单导出报 500",
             "消息类型": "文本", "消息发送时间": "2026年11月08日 14:30"},
            {"消息发送人": "李四", "消息内容": "收到",
             "消息类型": "文本", "消息发送时间": "2026年11月08日 14:31"},
        ]

        @staticmethod
        def dump_chat_history(friend, number, **kwargs):
            Messages.calls.append((friend, number))
            return list(Messages.records)

    utils = types.ModuleType("pyweixin.utils")
    utils.scan_for_new_messages = lambda **kw: {
        "项目A-线上问题": 3, "闲聊群": 1,
    }
    mod = types.ModuleType("pyweixin")
    mod.__path__ = []          # 当成包,importlib 才会去 sys.modules 里找子模块
    mod.Tools = Tools
    mod.Messages = Messages
    return mod, utils


def _install_fake_pyweixin(running: bool = True):
    mod, utils = _fake_pyweixin(running)
    old = {k: sys.modules.get(k) for k in ("pyweixin", "pyweixin.utils")}
    sys.modules["pyweixin"] = mod
    sys.modules["pyweixin.utils"] = utils
    wechat._SEEN.clear()
    return mod, old


def _restore_pyweixin(old: dict) -> None:
    for key, value in old.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value
    wechat._SEEN.clear()


def test_pyweixin_time_and_normalize() -> str:
    now = datetime(2026, 11, 8, 15, 0).timestamp()
    assert abs(wechat.parse_cn_time("2026年11月08日 14:30", now=now)
               - datetime(2026, 11, 8, 14, 30).timestamp()) < 1
    assert abs(wechat.parse_cn_time("昨天 09:05", now=now)
               - datetime(2026, 11, 7, 9, 5).timestamp()) < 1
    assert abs(wechat.parse_cn_time("星期一 10:00", now=now)
               - datetime(2026, 11, 2, 10, 0).timestamp()) < 1
    assert wechat.parse_cn_time("", now=now) == 0.0

    got = wechat.messages_from_pyweixin("项目A", [{
        "消息发送人": "张三", "消息内容": "导出报 500",
        "消息类型": "文本", "消息发送时间": "2026年11月08日 14:30",
    }], now=now)
    assert len(got) == 1
    assert got[0].group == "项目A" and got[0].via == "pyweixin"
    assert got[0].msg_id.startswith("wx")
    assert got[0].msg_type == "文本"
    assert wechat.messages_from_pyweixin("A", [{"消息内容": ""}]) == []

    # 同一份记录归一化两次,msg_id 必须一样(去重靠它)
    again = wechat.messages_from_pyweixin("项目A", [{
        "消息发送人": "张三", "消息内容": "导出报 500",
        "消息类型": "文本", "消息发送时间": "2026年11月08日 14:30",
    }], now=now)
    assert got[0].msg_id == again[0].msg_id
    return "解析:微信时间戳(含昨天/星期)与记录归一化都稳定"


def test_collect_with_fake_pyweixin() -> str:
    cfg = _cfg("collect")
    cfg.sources.wechat.backend = "pyweixin"
    cfg.sources.wechat.groups = ["项目A-线上问题"]
    cfg.sources.wechat.max_pull = 20
    mod, old = _install_fake_pyweixin()
    try:
        res = wechat.collect(cfg)
        assert not res.get("error"), res
        assert res["chats"] == 1 and res["messages"] == 2, res
        assert mod.Messages.calls == [("项目A-线上问题", 3)], mod.Messages.calls

        lines = wechat.spool_path(cfg).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["group"] == "项目A-线上问题" and first["via"] == "pyweixin"
        assert first["msg_id"].startswith("wx")

        # 同一批消息再采一次:进程内记住过,不再重复写
        res2 = wechat.collect(cfg)
        assert res2["messages"] == 0 and res2["skipped"] == 2, res2
        assert len(wechat.spool_path(cfg).read_text(encoding="utf-8").splitlines()) == 2

        # 不限分组时,未读的会话都采
        cfg.sources.wechat.groups = []
        wechat._SEEN.clear()
        res3 = wechat.collect(cfg)
        assert res3["chats"] == 2, res3
        assert {c for c, _ in mod.Messages.calls} == {"项目A-线上问题", "闲聊群"}
        return "采集:按分组/未读数拉消息,写进同一个 spool,重复不重写"
    finally:
        _restore_pyweixin(old)
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_collect_reports_environment_errors() -> str:
    cfg = _cfg("collecterr")
    cfg.sources.wechat.backend = "pyweixin"
    mod, old = _install_fake_pyweixin(running=False)
    try:
        res = wechat.collect(cfg)
        assert res.get("error") and "微信没在运行" in res["error"], res
        spool = wechat.spool_path(cfg)
        assert not spool.exists() or spool.read_text(encoding="utf-8") == ""
        return "采集:微信没启动时给一句人话,不抛异常"
    finally:
        _restore_pyweixin(old)
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_worker_collect_is_rate_limited() -> str:
    cfg = _cfg("rate")
    cfg.sources.wechat.backend = "pyweixin"
    cfg.sources.wechat.groups = ["项目A-线上问题"]
    cfg.sources.wechat.collect_interval = 300
    mod, old = _install_fake_pyweixin()
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            stats = {"wechat_collected": 0, "wechat_errors": 0}
            worker._maybe_collect_wechat(cfg, conn, stats)
            assert stats["wechat_collected"] == 2, stats
            assert db.meta_get(conn, "wechat:collect_at"), "应记下采集时间"
            calls = len(mod.Messages.calls)

            # 间隔没到,不再动 UI
            worker._maybe_collect_wechat(cfg, conn, stats)
            assert len(mod.Messages.calls) == calls
            assert stats["wechat_collected"] == 2

            # 关掉源就不采
            cfg.sources.wechat.enabled = False
            worker._maybe_collect_wechat(cfg, conn, stats)
            assert len(mod.Messages.calls) == calls
        finally:
            conn.close()
        return "调度:采集按 collect_interval 限频,关掉源就完全不碰微信"
    finally:
        _restore_pyweixin(old)
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_worker_collect_error_is_audited() -> str:
    cfg = _cfg("rateerr")
    cfg.sources.wechat.backend = "pyweixin"
    mod, old = _install_fake_pyweixin(running=False)
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            stats = {"wechat_collected": 0, "wechat_errors": 0}
            worker._maybe_collect_wechat(cfg, conn, stats)
            assert stats["wechat_errors"] == 1
            kinds = [r["kind"] for r in db.recent_audit(conn, 10)]
            assert "wechat_collect_error" in kinds, kinds
        finally:
            conn.close()
        return "失败:采集出错只记 audit,不拖垮这一轮 tick"
    finally:
        _restore_pyweixin(old)
        shutil.rmtree(cfg.root, ignore_errors=True)


def _make_fake_lib(root: Path) -> Path:
    """在磁盘上造一个假 pyweixin 包,给「子进程采集」那条路用。"""
    pkg = root / "fakelib" / "pyweixin"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "from .WeChatAuto import Tools, Messages\nfrom . import utils\n",
        encoding="utf-8")
    (pkg / "WeChatAuto.py").write_text(
        "class Tools:\n"
        "    @staticmethod\n"
        "    def is_weixin_running():\n"
        "        return True\n"
        "    @staticmethod\n"
        "    def get_weixin_hwnd():\n"
        "        return 0\n"
        "\n"
        "class Messages:\n"
        "    @staticmethod\n"
        "    def dump_chat_history(friend, number, **kw):\n"
        "        return [{'消息发送人': '张三', '消息内容': '订单导出报 500',\n"
        "                 '消息类型': '文本', '消息发送时间': '2026年11月08日 14:30'}]\n",
        encoding="utf-8")
    (pkg / "utils.py").write_text(
        "def scan_for_new_messages(**kw):\n"
        "    return {'项目A-线上问题': 2, '闲聊群': 1}\n",
        encoding="utf-8")
    return root / "fakelib"


def test_collector_script_protocol() -> str:
    """独立采集脚本:只认 pyweixin,结果写 JSON 文件,不靠 stdout。"""
    cfg = _cfg("script")
    lib = _make_fake_lib(cfg.root)
    script = Path(wechat.__file__).with_name("rpa_collector.py")
    out = cfg.root / "collect.json"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--out", str(out),
             "--library-path", str(lib), "--groups", "项目A-线上问题",
             "--max-pull", "5"],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr[-500:])
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["unread"] == {"项目A-线上问题": 2, "闲聊群": 1}, payload
        assert list(payload["chats"]) == ["项目A-线上问题"], payload
        assert payload["chats"]["项目A-线上问题"][0]["消息内容"] == "订单导出报 500"

        # --check:假包没有微信窗口,应该给出可读结论而不是抛栈
        check_out = cfg.root / "check.json"
        subprocess.run(
            [sys.executable, str(script), "--out", str(check_out),
             "--library-path", str(lib), "--check"],
            capture_output=True, text=True, timeout=60,
        )
        result = json.loads(check_out.read_text(encoding="utf-8"))
        assert result["ok"] is False and "找不到微信窗口" in result["detail"], result
        return "脚本:未读扫描/取记录/环境检查的 JSON 协议都能独立跑"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_collect_via_subprocess() -> str:
    """配了 python_exe 就走子进程:dots 和 pyweixin 的依赖可以不在一个环境。"""
    cfg = _cfg("subproc")
    cfg.sources.wechat.backend = "pyweixin"
    cfg.sources.wechat.groups = ["项目A-线上问题"]
    cfg.sources.wechat.python_exe = sys.executable
    cfg.sources.wechat.collect_timeout = 60
    cfg.sources.wechat.library_path = str(_make_fake_lib(cfg.root))
    try:
        res = wechat.collect(cfg)
        assert not res.get("error"), res
        assert res["messages"] == 1, res
        lines = wechat.spool_path(cfg).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        msg = json.loads(lines[0])
        assert msg["group"] == "项目A-线上问题" and msg["via"] == "pyweixin"

        res2 = wechat.collect(cfg)
        assert res2["messages"] == 0 and res2["skipped"] == 1, res2
        return "子进程:换解释器采集也能落到同一个 spool,重复消息不重写"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def run() -> str:
    notes = [
        test_spool_incremental(),
        test_poll_only_bugs_and_groups(),
        test_webhook(),
        test_route_dedupes_into_one_goal(),
        test_auto_pipeline(),
        test_pyweixin_time_and_normalize(),
        test_collect_with_fake_pyweixin(),
        test_collect_reports_environment_errors(),
        test_worker_collect_is_rate_limited(),
        test_worker_collect_error_is_audited(),
        test_collector_script_protocol(),
        test_collect_via_subprocess(),
    ]
    return "微信群:" + ";".join(notes)


def test_wechat():                          # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
