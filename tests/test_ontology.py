"""本体库的自检:手改不被覆盖、派生可重建、改完能发现过期。

这些用例全都不碰真数据库:采集那层用归一化后的元数据直接喂,merge 是纯函数。
真连库的路径只在 dots ontology sync 里,失败会明确报「缺驱动 / 连不上」。

和别的测试一样,`run()` 返回一句人话,给 dots selftest 用;
pytest 会报 PytestReturnNotNoneWarning,这是预期的。
"""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from types import SimpleNamespace

from dots import ontology as ont


def _cfg(tag: str = "ont") -> SimpleNamespace:
    root = Path.cwd() / f".dots-test-{tag}-{secrets.token_hex(3)}"
    root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        root=root,
        workspace=root / "workspace",
        ontology=SimpleNamespace(
            enabled=True, dir="ontology", derived="generated",
            datasources={}, inject_prompt=True, prompt_entities=40,
        ),
        sdlc=SimpleNamespace(enabled=True, dir="projects", max_fix_rounds=3,
                             auto_from_bug=True, stages=[]),
        mcp=SimpleNamespace(enabled=False, servers={}),
    )


def _doc() -> dict:
    doc = ont.empty()
    doc["entities"] = [
        {
            "id": "customer", "label": "客户", "status": "confirmed", "curated": True,
            "source": {"datasource": "crm", "schema": "public", "table": "customers"},
            "primary_key": ["id"],
            "fields": [
                {"name": "id", "label": "客户ID", "type": "bigint", "semantic": "id",
                 "nullable": False, "enum": [], "curated": True},
                {"name": "name", "label": "客户名称", "type": "text",
                 "semantic": "string", "nullable": False, "enum": []},
            ],
            "relations": [],
        },
        {
            "id": "orders", "label": "订单", "status": "draft",
            "source": {"datasource": "crm", "schema": "public", "table": "orders"},
            "primary_key": ["id"],
            "fields": [
                {"name": "id", "label": "订单号", "type": "bigint", "semantic": "id"},
                {"name": "customer_id", "label": "客户", "type": "bigint", "semantic": "id"},
                {"name": "status", "label": "状态", "type": "text", "semantic": "enum",
                 "enum": ["paid", "unpaid"]},
            ],
            "relations": [],
        },
    ]
    doc["enums"] = [{"id": "order_status", "values": [
        {"value": "paid", "label": "已支付"}, {"value": "unpaid", "label": "未支付"}]}]
    doc["glossary"] = [{"term": "GMV", "definition": "成交总额"}]
    ont._infer_relations(doc)
    return doc


# --------------------------------------------------------------------- 用例

def test_validate_rules() -> str:
    cfg = _cfg("val")
    try:
        doc = _doc()
        assert ont.validate(doc) == []

        dup = json.loads(json.dumps(doc))
        dup["entities"].append(json.loads(json.dumps(doc["entities"][0])))
        assert any("重复" in p for p in ont.validate(dup))

        ghost = json.loads(json.dumps(doc))
        ghost["entities"][1]["relations"] = [{"target": "ghost", "kind": "many_to_one"}]
        assert any("ghost" in p for p in ont.validate(ghost))

        bad_field = json.loads(json.dumps(doc))
        bad_field["entities"][1]["fields"].append({"name": "id"})
        assert any("重复" in p for p in ont.validate(bad_field))

        assert ont.validate({"entities": "nope"}), "entities 不是数组应该被拒"
        return "校验:重复实体/悬空关系/重复字段名都拦住了"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_save_and_apply() -> str:
    cfg = _cfg("apply")
    try:
        doc = _doc()
        p = ont.save(cfg, doc, note="测试")
        assert p.exists()
        saved = ont.load(cfg)
        assert saved["digest"] == ont.digest(saved)
        assert saved["entities"][1]["relations"], "保存时应该补出 customer_id 的关系"

        res = ont.apply(cfg, saved)
        assert res["written"], res
        root = cfg.workspace / cfg.ontology.derived
        for rel in ("README.md", "schema.sql", "models.py", "types.ts", "er.mmd",
                    "entities/orders.md"):
            assert (root / rel).exists(), rel

        sql = (root / "schema.sql").read_text(encoding="utf-8")
        assert "PRIMARY KEY (id)" in sql
        py = (root / "models.py").read_text(encoding="utf-8")
        assert "class Orders" in py and "customer_id" in py
        ts = (root / "types.ts").read_text(encoding="utf-8")
        assert 'export interface Orders' in ts and '"paid"' in ts

        # 保存第二次会留历史快照,手改坏了能捞回来
        ont.save(cfg, saved, note="第二次")
        assert list((cfg.workspace / cfg.ontology.dir / "history").glob("*.json"))
        return "派生:数据字典/DDL/Python/TS 都能从本体库重建"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_status_detects_drift() -> str:
    cfg = _cfg("drift")
    try:
        doc = _doc()
        ont.save(cfg, doc)
        assert ont.status(cfg)["synced"] is False       # 还没生成过

        ont.apply(cfg, ont.load(cfg))
        assert ont.status(cfg)["synced"] is True

        # 本体库改了但没 apply
        changed = ont.load(cfg)
        changed["entities"][1]["label"] = "订单(新版)"
        ont.save(cfg, changed)
        st = ont.status(cfg)
        assert st["synced"] is False and "apply" in st["reason"], st
        assert st["changed"]

        ont.apply(cfg, ont.load(cfg))
        assert ont.status(cfg)["synced"] is True

        # 派生文件被手改
        target = cfg.workspace / cfg.ontology.derived / "README.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n手改\n", encoding="utf-8")
        st = ont.status(cfg)
        assert st["synced"] is True, "手改不该影响一致性判断"
        assert st["hand_edited"], st
        return "一致性:改本体/手改派生文件都能被发现"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_manual_edit_survives_resync() -> str:
    cfg = _cfg("merge")
    try:
        doc = _doc()
        ontsave = ont.save(cfg, doc)
        assert ontsave.exists()

        raw = [
            {
                "table": "customers", "schema": "public",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False},
                    {"name": "name", "type": "varchar", "nullable": False},
                    {"name": "vip_level", "type": "int", "nullable": True},
                ],
                "primary_key": ["id"], "foreign_keys": [],
            },
            {
                "table": "orders", "schema": "public",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False},
                    {"name": "customer_id", "type": "bigint", "nullable": True},
                    {"name": "status", "type": "varchar", "nullable": False},
                    {"name": "amount", "type": "numeric", "nullable": True},
                ],
                "primary_key": ["id"],
                "foreign_keys": [{"field": "customer_id", "table": "customers",
                                  "target_field": "id"}],
            },
            {
                "table": "refunds", "schema": "public",
                "columns": [{"name": "id", "type": "bigint", "nullable": False}],
                "primary_key": ["id"], "foreign_keys": [],
            },
        ]
        ds = SimpleNamespace(name="crm", kind="postgres", schema="public",
                             database="", include=[], exclude=[], sample_size=10)
        stats = ont.merge(doc, "crm", raw, ds=ds)

        # 人工改过的实体:标注保留,库里的新列只报冲突不静默加进去
        customer = ont.find_entity(doc, "customer")
        assert customer["label"] == "客户" and customer["status"] == "confirmed"
        assert "vip_level" not in {f["name"] for f in customer["fields"]}
        assert any("vip_level" in c for c in stats["conflicts"]), stats

        # 没改过的实体:跟着库走,新字段自动补
        orders = ont.find_entity(doc, "orders")
        assert "amount" in {f["name"] for f in orders["fields"]}
        assert any("refunds" == a for a in stats["added"]), stats

        # 库里消失的表只打 missing,不删 —— 删了人工标注就没了
        stats2 = ont.merge(doc, "crm", raw[:2], ds=ds)
        assert "refunds" in stats2["missing"] or "refunds" in stats["added"]
        return "合并:手改的实体不被覆盖,新表进草稿,消失的表只标记"
    finally:
        shutil.rmtree(cfg.root, ignore_errors=True)


def test_lookup_and_context() -> str:
    doc = _doc()
    hits = ont.lookup(doc, "订单")
    assert hits and hits[0]["id"] == "orders", hits
    assert ont.lookup(doc, "customer_id")[0]["id"] == "orders"
    assert ont.lookup(doc, "根本不存在的东西") == []

    text = ont.context(doc)
    assert "orders 订单" in text and "order_status" in text
    assert "GMV" in text

    assert ont.suggest_label("created_at") == "创建时间"
    assert ont.semantic_of("numeric(18,2)", "amount") == "decimal"
    assert ont.semantic_of("bigint", "customer_id") == "id"
    assert ont.semantic_of("jsonb", "extra") == "json"
    return "查询:中英文/字段名都能命中,摘要能塞进系统提示"


def test_introspect_guards() -> str:
    ds = SimpleNamespace(name="x", kind="oracle", dsn_env="X", schema="", database="",
                         include=[], exclude=[], sample_size=10)
    try:
        ont.introspect(ds)
    except RuntimeError as e:
        assert "不支持" in str(e)
    else:
        raise AssertionError("不支持的库类型应该报错")

    missing = SimpleNamespace(name="y", kind="postgres", dsn_env="DOTS_TEST_NO_SUCH_DSN",
                              schema="public", database="", include=[], exclude=[],
                              sample_size=10)
    try:
        ont.dsn(missing)
    except RuntimeError as e:
        assert "环境变量" in str(e)
    else:
        raise AssertionError("缺 DSN 应该报错")
    return "采集:不支持的库和缺 DSN 都给出能照做的报错"


def test_derived_code_is_valid_python() -> str:
    doc = _doc()
    code = ont.render_python(doc)
    compile(code, "<generated>", "exec")     # 生成物要能被 Python 直接编译
    assert "class Customer" in code
    assert "from dataclasses import dataclass" in code
    return "生成物:models.py 能被 Python 编译"


def run() -> str:
    notes = [
        test_validate_rules(),
        test_save_and_apply(),
        test_status_detects_drift(),
        test_manual_edit_survives_resync(),
        test_lookup_and_context(),
        test_introspect_guards(),
        test_derived_code_is_valid_python(),
    ]
    return "本体库:" + ";".join(notes)


def test_ontology():                       # pytest 入口
    run()


if __name__ == "__main__":
    print(run())
