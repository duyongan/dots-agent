"""流水线的自检:阶段推进靠产物、测试结论驱动修复循环、改本体后文档跟着变。

这里不跑模型:阶段目标由 db 直接标成 done,推进逻辑照样走真的 worker 钩子
(sdlc.on_goal_done),所以测的是「产物齐了才推进」这条真规则。
"""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from types import SimpleNamespace

from dots import db, ontology as ont, sdlc


def _cfg(tag: str = "sdlc") -> SimpleNamespace:
    root = Path.cwd() / f".dots-test-{tag}-{secrets.token_hex(3)}"
    root.mkdir(parents=True, exist_ok=True)
    cfg = SimpleNamespace(
        root=root,
        workspace=root / "workspace",
        db=root / "dot.db",
        sdlc=SimpleNamespace(enabled=True, dir="projects", max_fix_rounds=3,
                             auto_from_bug=True, stages=[]),
        ontology=SimpleNamespace(enabled=False, dir="ontology", derived="generated",
                                 datasources={}, inject_prompt=True, prompt_entities=20),
        mcp=SimpleNamespace(enabled=False, servers={}),
    )
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


CONTENT: dict[str, dict[str, str]] = {
    "requirements": {
        "PRD/requirements.md": "# 需求\n\n## 背景\nx\n## 用户故事\nx\n## 验收标准\nx\n"
                               "## 非目标\nx\n## 待确认问题\nx\n",
        "PRD/questions.md": "# 澄清\n\n## 问题\n1. 导出字段?\n",
    },
    "prototype": {
        "design/prototype.md": "# 原型\n\n## 页面\nx\n## 交互\nx\n## 状态\nx\n",
        "design/prototype.html": "<!doctype html><html><body>demo</body></html>\n",
    },
    "dev": {
        "dev/spec.md": "# 方案\n\n## 方案\nx\n## 任务拆分\n- T01\n",
        "dev/notes.md": "# 说明\n\n## 怎么跑\n`python -m app`\n",
    },
    "test": {
        "test/report.md": "# 测试\n\n| 用例 | 结果 |\n| --- | --- |\n| 导出 | 通过 |\n\n"
                          "VERDICT: {verdict}\n",
    },
    "fix": {
        "fix/fix-log.md": "# 修复\n\n## 缺陷\nx\n## 根因\nx\n## 改动\nx\n## 回归\nx\n",
    },
    "archive": {
        "archive/summary.md": "# 归档\n\n## 功能点\nx\n## 产物\nx\n## 上下文\nx\n",
    },
}


def _write(cfg, p, stage, *, verdict="PASS") -> None:
    base = cfg.workspace / cfg.sdlc.dir / p["slug"]
    for rel, text in CONTENT[stage].items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(verdict=verdict), encoding="utf-8")


def _finish(cfg, conn, p, stage, *, verdict="PASS") -> tuple[dict, str | None]:
    """把当前阶段的目标标成完成(带证据),再走一遍真的推进钩子。"""
    _write(cfg, p, stage, verdict=verdict)
    gid = p["stages"][stage]["goal_id"]
    db.set_goal(conn, gid, status=db.DONE, summary="阶段完成", evidence="产物已落盘")
    note = sdlc.on_goal_done(cfg, conn, db.get_goal(conn, gid))
    return sdlc.load(cfg, p["id"]), note


def test_create_and_goal_text() -> str:
    cfg = _cfg("create")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "把订单导出成 CSV,只要已支付的",
                            title="订单导出", external={"jira": "OPS-12"})
            assert (cfg.workspace / cfg.sdlc.dir / p["slug"] / "pipeline.json").exists()
            assert p["track"] == "feature" and p["stage"] == "requirements"

            goals = db.list_goals(conn)
            assert len(goals) == 1
            text = goals[0]["text"]
            assert "【SDLC" in text and p["id"] in text
            assert "PRD/requirements.md" in text and "## 验收标准" in text
            assert goals[0]["bot"] == "pm"
            assert "OPS-12" in json.dumps(p, ensure_ascii=False)
            return "创建:目录、pipeline.json、首阶段目标(带产物清单)都齐"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_missing_artifacts_blocks() -> str:
    cfg = _cfg("gate")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "做一个报表页")
            # 什么都没写就标完成
            gid = p["stages"]["requirements"]["goal_id"]
            db.set_goal(conn, gid, status=db.DONE, summary="嘴上说完了", evidence="??")
            note = sdlc.on_goal_done(cfg, conn, db.get_goal(conn, gid))
            assert note and "重派" in note, note

            p2 = sdlc.load(cfg, p["id"])
            assert p2["stage"] == "requirements"
            assert p2["stages"]["requirements"]["attempts"] == 2
            # 重派后确实多了一个目标
            assert len(db.list_goals(conn)) == 2

            # 第二次还不落盘 -> 停住,等人看
            gid2 = p2["stages"]["requirements"]["goal_id"]
            db.set_goal(conn, gid2, status=db.DONE, summary="又没写", evidence="??")
            note2 = sdlc.on_goal_done(cfg, conn, db.get_goal(conn, gid2))
            assert note2 and "停下" in note2, note2
            p3 = sdlc.load(cfg, p["id"])
            assert p3["status"] == "blocked"
            return "门槛:产物不齐重派一次,两次不成就停住"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_happy_path() -> str:
    cfg = _cfg("happy")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "把订单导出成 CSV")
            expected = {"requirements": "prototype", "prototype": "dev",
                        "dev": "test"}
            for stage, nxt in expected.items():
                p, note = _finish(cfg, conn, p, stage)
                assert note and p["stage"] == nxt, (stage, note, p["stage"])
            p, note = _finish(cfg, conn, p, "test", verdict="PASS")
            assert p["stage"] == "archive", note
            p, note = _finish(cfg, conn, p, "archive")
            assert p["status"] == "done", note
            return "主流程:需求→原型→开发→测试(PASS)→归档,一路自动推进"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_fail_goes_to_fix_and_back() -> str:
    cfg = _cfg("fail")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "导出的 CSV 缺一列")
            for stage in ("requirements", "prototype", "dev"):
                p, _ = _finish(cfg, conn, p, stage)

            p, note = _finish(cfg, conn, p, "test", verdict="FAIL")
            assert p["stage"] == "fix" and p["rounds"]["fix"] == 1, note

            p, note = _finish(cfg, conn, p, "fix")
            assert p["stage"] == "test" and p["rounds"]["test"] == 1, note

            p, note = _finish(cfg, conn, p, "test", verdict="PASS")
            assert p["stage"] == "archive", note
            return "修复循环:FAIL 进修复,修完再测,PASS 才归档"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_fix_round_limit() -> str:
    cfg = _cfg("limit")
    cfg.sdlc.max_fix_rounds = 1
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "导出一直失败")
            for stage in ("requirements", "prototype", "dev"):
                p, _ = _finish(cfg, conn, p, stage)
            p, _ = _finish(cfg, conn, p, "test", verdict="FAIL")
            assert p["stage"] == "fix"
            p, _ = _finish(cfg, conn, p, "fix")
            p, note = _finish(cfg, conn, p, "test", verdict="FAIL")
            assert p["status"] == "blocked", note
            assert p["stage"] == "test"
            return "上限:修到 max_fix_rounds 还 FAIL 就停,不无限烧 token"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_bugfix_track_marks_bugs_fixed() -> str:
    cfg = _cfg("bugfix")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            from dots import bugs as bugs_mod
            bug, _, _ = bugs_mod.record(conn, "线上导出接口 500")
            p = sdlc.create(cfg, conn, bugs_mod.requirement_text([bug]),
                            title="[BUG]导出 500", track="bugfix",
                            bug_ids=[bug["id"]])
            assert p["stage"] == "fix"
            db.set_bug(conn, bug["id"], status=db.BUG_FIXING, pipeline_id=p["id"])

            p, _ = _finish(cfg, conn, p, "fix")
            assert p["stage"] == "test"
            p, note = _finish(cfg, conn, p, "test", verdict="PASS")
            assert p["stage"] == "archive", note
            p, note = _finish(cfg, conn, p, "archive")
            assert p["status"] == "done"
            assert db.get_bug(conn, bug["id"])["status"] == db.BUG_FIXED
            return "BUG 轨迹:修复→测试→归档,归档时台账自动置为已修复"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_ontology_blocks_and_sync() -> str:
    cfg = _cfg("ontsync")
    cfg.ontology.enabled = True
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            doc = ont.empty()
            doc["entities"] = [{
                "id": "orders", "label": "订单", "status": "confirmed",
                "source": {"datasource": "crm", "schema": "public", "table": "orders"},
                "primary_key": ["id"],
                "fields": [{"name": "id", "label": "订单号", "semantic": "id"},
                           {"name": "status", "label": "状态", "semantic": "enum",
                            "enum": ["paid", "unpaid"]}],
                "relations": [],
            }]
            ont.save(cfg, doc)

            p = sdlc.create(cfg, conn, "查订单状态")
            text = db.get_goal(conn, p["stages"]["requirements"]["goal_id"])["text"]
            assert "本体库" in text and "orders" in text, "阶段目标应带本体摘要"

            _write(cfg, p, "requirements")
            changed = sdlc.sync_docs(cfg, ont.load(cfg))
            assert changed["changed"], changed
            md_path = cfg.workspace / cfg.sdlc.dir / p["slug"] / "PRD/requirements.md"
            md = md_path.read_text(encoding="utf-8")
            assert "订单" in md and "ontology:begin" in md

            # 再跑一次不该重复追加
            before = md
            sdlc.sync_docs(cfg, ont.load(cfg))
            after = md_path.read_text(encoding="utf-8")
            assert before == after
            return "本体同步:阶段目标带摘要,文档里自动生成段落且可重复执行"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_manual_advance() -> str:
    cfg = _cfg("manual")
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "人工推进演示")
            msg = sdlc.advance_manual(cfg, conn, p, note="产物在别处")
            assert "prototype" in msg, msg
            p2 = sdlc.load(cfg, p["id"])
            assert p2["stage"] == "prototype"
            assert any(h.get("event") == "stage_forced" for h in p2["history"])
            return "人工:卡住时能跳过当前阶段,并留下是谁跳的"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_mcp_hint_in_goal() -> str:
    """接上 Jira 之后,阶段目标里要明确写「单据以 Jira 为准」。"""
    cfg = _cfg("mcphint")
    cfg.mcp = SimpleNamespace(enabled=True, servers={"jira": SimpleNamespace(
        name="jira", enabled=True, transport="stdio", command="uvx",
        args=["mcp-atlassian"], url="", env={}, pass_env=[], timeout=5.0,
        prefix="jira")})
    try:
        conn = db.connect(cfg.db)
        db.init(conn)
        try:
            p = sdlc.create(cfg, conn, "把订单导出成 CSV")
            text = db.get_goal(conn, p["stages"]["requirements"]["goal_id"])["text"]
            assert "外部系统" in text and "jira" in text, text[-400:]
            assert "Jira 单" in text or "单号" in text
            return "MCP 提示:阶段目标里写清了哪个系统是单据正本"
        finally:
            conn.close()
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def run() -> str:
    notes = [
        test_create_and_goal_text(),
        test_missing_artifacts_blocks(),
        test_happy_path(),
        test_fail_goes_to_fix_and_back(),
        test_fix_round_limit(),
        test_bugfix_track_marks_bugs_fixed(),
        test_ontology_blocks_and_sync(),
        test_mcp_hint_in_goal(),
        test_manual_advance(),
    ]
    return "流水线:" + ";".join(notes)


def test_sdlc():                            # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
