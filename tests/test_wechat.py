"""微信群监控的自检:spool 增量读、half-line、webhook、BUG 去重派单。

不登录微信,也不跑真桥 —— 测的是 dots 这一侧:契约稳不稳、半行会不会丢、
十个人报同一条错会不会变成十个目标。
"""

from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
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
                enabled=True, spool="wechat/inbox.jsonl", groups=[], keywords=[],
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


def run() -> str:
    notes = [
        test_spool_incremental(),
        test_poll_only_bugs_and_groups(),
        test_webhook(),
        test_route_dedupes_into_one_goal(),
        test_auto_pipeline(),
    ]
    return "微信群:" + ";".join(notes)


def test_wechat():                          # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
