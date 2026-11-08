"""本体库:从公司库采元数据 -> 一份可手改的 JSON -> 派生文档和代码。

三层,各管各的:

  物理层  公司库里的表/集合/字段。只读。introspect() 只做「看一眼」,不写业务库。
  语义层  ontology/ontology.json。实体、字段、关系、枚举、术语。人可以手改,
          手改过的实体标 curated=true,下一次采集不会覆盖你的标注。
  派生层  generated/ 下的数据字典、ER 图、DDL、Python/TS 模型。整体可重建,
          所以别手改 —— 改本体,再 `dots ontology apply`。

整个流程靠的就是这份 JSON:需求分析拿它对齐字段,原型拿它对齐实体,
开发拿它对齐命名,测试拿它验枚举值。它一变,派生的文档和代码跟着变,
流水线里各阶段的「数据模型」段落也跟着重写。

没有数据库也能用:手写 entities 就是一份纯手工本体库。
这条路径有测试盯着,不依赖任何驱动。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ONTOLOGY_FILE = "ontology.json"
SCHEMA_VERSION = 1

# 派生文件里放这个标记,方便人一眼看出「这是生成物,别手改」。
GENERATED_BANNER = "<!-- 由 dots ontology apply 生成,别手改;改 ontology.json 再重新生成 -->"

RELATION_KINDS = ("many_to_one", "one_to_many", "one_to_one", "many_to_many")
ENTITY_STATUSES = ("draft", "confirmed", "deprecated")

# 字段名的中文词表。只影响「默认建议」,人工改过就不再覆盖。
_LABEL_WORDS: dict[str, str] = {
    "id": "编号", "uuid": "唯一标识", "name": "名称", "title": "标题",
    "status": "状态", "state": "状态", "type": "类型", "kind": "类型",
    "desc": "描述", "description": "描述", "remark": "备注", "note": "备注",
    "amount": "金额", "price": "价格", "total": "总额", "total_amount": "总金额",
    "quantity": "数量", "count": "数量", "num": "数量", "qty": "数量",
    "user": "用户", "user_id": "用户", "customer": "客户", "customer_id": "客户",
    "order": "订单", "order_id": "订单", "product": "产品", "product_id": "产品",
    "sku": "SKU", "shop": "店铺", "store": "门店", "dept": "部门",
    "created_at": "创建时间", "updated_at": "更新时间", "deleted_at": "删除时间",
    "create_time": "创建时间", "update_time": "更新时间", "ts": "时间",
    "time": "时间", "date": "日期", "start": "开始", "end": "结束",
    "phone": "手机号", "mobile": "手机号", "email": "邮箱", "address": "地址",
    "code": "编码", "no": "编号", "sn": "序列号", "url": "链接",
    "avatar": "头像", "img": "图片", "image": "图片", "file": "文件",
    "version": "版本", "level": "级别", "score": "分数", "rank": "排名",
    "is_deleted": "是否删除", "is_active": "是否启用", "enabled": "是否启用",
    "owner": "负责人", "creator": "创建人", "operator": "操作人",
}

# 简单类型 -> 语义类型。顺序有意义:先匹配到的赢。
_SEMANTIC_RULES: list[tuple[tuple[str, ...], str]] = [
    (("bool",), "boolean"),
    (("timestamp", "datetime", "date", "time"), "datetime"),
    (("numeric", "decimal", "money", "float", "double", "real"), "decimal"),
    (("json", "jsonb", "object"), "json"),
    (("bytea", "blob", "binary"), "binary"),
    (("int", "serial", "long"), "integer"),
    (("char", "text", "string", "varchar", "uuid", "enum"), "string"),
]

_ID_NAME = re.compile(r"(^|_)id$", re.I)
_SNAKE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_DDL_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


# ------------------------------------------------------------------ 路径与读写

def directory(cfg) -> Path:
    return cfg.workspace / cfg.ontology.dir


def path(cfg) -> Path:
    return directory(cfg) / ONTOLOGY_FILE


def empty() -> dict:
    """空本体库。version 和 entities 是仅有的两个必填项。"""
    return {
        "version": SCHEMA_VERSION,
        "updated_at": "",
        "sources": {},
        "entities": [],
        "enums": [],
        "glossary": [],
        "apis": [],
    }


def load(cfg) -> dict:
    """读本体库。没有文件返回空库 —— 调用方不用先判断文件在不在。"""
    p = path(cfg)
    if not p.exists():
        return empty()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{p} 读不了:{type(e).__name__}: {e}")
    if not isinstance(doc, dict):
        raise SystemExit(f"{p} 顶层应该是 JSON 对象,现在是 {type(doc).__name__}。")
    doc.setdefault("version", SCHEMA_VERSION)
    doc.setdefault("entities", [])
    doc.setdefault("enums", [])
    doc.setdefault("glossary", [])
    doc.setdefault("apis", [])
    doc.setdefault("sources", {})
    return doc


def save(cfg, doc: dict, *, note: str = "") -> Path:
    """原子写 + 留一份历史快照。手改坏的 JSON 还能从这里捞回来。"""
    problems = validate(doc)
    if problems:
        raise SystemExit("本体库没存:校验不过\n  - " + "\n  - ".join(problems))

    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        _snapshot(cfg, p)
    doc = dict(doc)
    doc["version"] = int(doc.get("version") or SCHEMA_VERSION)
    doc["updated_at"] = _now()
    if note:
        doc["last_note"] = note
    # digest 要在所有字段都写完之后再算,而且 digest/updated_at 自己不参与
    # —— 否则存一次指纹就变一次,status 永远报「待更新」。
    doc["digest"] = digest(doc)
    _atomic_write(p, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return p


def _snapshot(cfg, p: Path, keep: int = 20) -> None:
    hist = directory(cfg) / "history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    try:
        shutil.copy2(p, hist / f"{stamp}.json")
    except OSError:
        return
    old = sorted(hist.glob("*.json"))
    for stale in old[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass


def _atomic_write(p: Path, text: str) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):        # Windows 上 os.replace 会被占用方挡住,重试
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------- 校验

def validate(doc: dict) -> list[str]:
    """返回能直接打给人看的错误清单。空列表 = 可以落盘。"""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["顶层必须是对象"]

    entities = doc.get("entities")
    if not isinstance(entities, list):
        return ["entities 必须是数组"]

    seen: dict[str, int] = {}
    for i, ent in enumerate(entities):
        where = f"entities[{i}]"
        if not isinstance(ent, dict):
            problems.append(f"{where} 不是对象")
            continue
        eid = str(ent.get("id") or "").strip()
        if not eid:
            problems.append(f"{where} 缺 id")
            continue
        if eid in seen:
            problems.append(f"实体 id 重复:{eid}(第 {seen[eid]} 个和第 {i} 个)")
        seen[eid] = i
        if not str(ent.get("label") or "").strip():
            problems.append(f"实体 {eid} 缺 label(中文名)")
        if ent.get("status") and ent["status"] not in ENTITY_STATUSES:
            problems.append(f"实体 {eid} 的 status={ent['status']!r},只能是 {ENTITY_STATUSES}")

        fields = ent.get("fields") or []
        if not isinstance(fields, list):
            problems.append(f"实体 {eid} 的 fields 必须是数组")
            fields = []
        fnames: set[str] = set()
        for j, fld in enumerate(fields):
            if not isinstance(fld, dict):
                problems.append(f"实体 {eid} 的 fields[{j}] 不是对象")
                continue
            fname = str(fld.get("name") or "").strip()
            if not fname:
                problems.append(f"实体 {eid} 的 fields[{j}] 缺 name")
                continue
            if fname in fnames:
                problems.append(f"实体 {eid} 的字段 {fname} 重复")
            fnames.add(fname)

        for k, rel in enumerate(ent.get("relations") or []):
            if not isinstance(rel, dict):
                problems.append(f"实体 {eid} 的 relations[{k}] 不是对象")
                continue
            target = str(rel.get("target") or "").strip()
            if target and target not in seen and target not in {
                str(e.get("id")) for e in entities if isinstance(e, dict)
            }:
                problems.append(f"实体 {eid} 的关系指向不存在的 {target!r}")
            kind = rel.get("kind") or "many_to_one"
            if kind not in RELATION_KINDS:
                problems.append(f"实体 {eid} 的关系类型 {kind!r},只能是 {RELATION_KINDS}")

    for k, en in enumerate(doc.get("enums") or []):
        if not isinstance(en, dict) or not en.get("id"):
            problems.append(f"enums[{k}] 缺 id")
    return problems


def digest(doc: dict) -> str:
    """本体库的指纹。updated_at / digest 自己不参与,不然每存一次都变。"""
    body = {k: v for k, v in doc.items() if k not in ("updated_at", "digest")}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ 实体访问

def find_entity(doc: dict, key: str) -> dict | None:
    """按 id、物理表名、或中文名找实体。大小写不敏感。"""
    want = (key or "").strip().lower()
    if not want:
        return None
    for ent in doc.get("entities") or []:
        names = {
            str(ent.get("id", "")).lower(),
            str(ent.get("label", "")).lower(),
            str((ent.get("source") or {}).get("table", "")).lower(),
        }
        if want in names:
            return ent
    return None


def entity_map(doc: dict) -> dict[str, dict]:
    return {str(e.get("id")): e for e in doc.get("entities") or [] if e.get("id")}


def lookup(doc: dict, query: str, limit: int = 5) -> list[dict]:
    """按实体名/字段名/描述搜。给 agent 的 ontology_lookup 工具用。

    返回的是实体原文(带字段和关系),不是摘要 —— 模型要的就是准确的字段名。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[tuple[int, dict]] = []
    for ent in doc.get("entities") or []:
        text = " ".join(
            str(x) for x in (
                ent.get("id"), ent.get("label"), ent.get("description"),
                ent.get("domain"), ent.get("owner"),
            )
        ).lower()
        score = 0
        if q == str(ent.get("id", "")).lower() or q == str(ent.get("label", "")).lower():
            score = 100
        elif q in text:
            score = 60
        else:
            for fld in ent.get("fields") or []:
                fname = str(fld.get("name", "")).lower()
                flabel = str(fld.get("label", "")).lower()
                if q == fname:
                    score = max(score, 50)
                elif q in fname or q in flabel:
                    score = max(score, 30)
        if score:
            hits.append((score, ent))
    hits.sort(key=lambda x: (-x[0], str(x[1].get("id"))))
    return [e for _, e in hits[:limit]]


def context(doc: dict, limit: int = 40) -> str:
    """压成能塞进系统提示的摘要。索引,不是全部 —— 细节靠 ontology_lookup 现查。"""
    ents = doc.get("entities") or []
    if not ents:
        return ""
    lines = [f"实体 {len(ents)} 个(这里是索引,查细节用 ontology_lookup):"]
    for ent in ents[:limit]:
        fields = ent.get("fields") or []
        bits = []
        for fld in fields[:8]:
            label = fld.get("label") or fld.get("name")
            enum = fld.get("enum") or []
            if enum:
                bits.append(f"{fld.get('name')}({label}:{'|'.join(map(str, enum[:4]))})")
            else:
                bits.append(f"{fld.get('name')}({label})")
        more = f" 等 {len(fields)} 个字段" if len(fields) > 8 else ""
        rels = []
        for rel in (ent.get("relations") or [])[:3]:
            rels.append(f"{rel.get('field')}->{rel.get('target')}")
        rel_text = f"; 关系 {', '.join(rels)}" if rels else ""
        lines.append(
            f"- {ent.get('id')} {ent.get('label')}{more}:{', '.join(bits)}{rel_text}"
        )
    if len(ents) > limit:
        lines.append(f"(还有 {len(ents) - limit} 个实体没列出来,用 ontology_lookup 搜)")
    enums = doc.get("enums") or []
    if enums:
        enum_bits = []
        for en in enums[:12]:
            values = [str(v.get("value")) for v in (en.get("values") or [])][:8]
            enum_bits.append(f"{en.get('id')}={','.join(values)}")
        lines.append("枚举:" + "; ".join(enum_bits))
    terms = doc.get("glossary") or []
    if terms:
        lines.append("术语:" + "; ".join(
            f"{t.get('term')}={t.get('definition', '')[:30]}" for t in terms[:8]
        ))
    return "\n".join(lines)


# -------------------------------------------------------------------- 采集

def normalize_kind(kind: str) -> str:
    text = (kind or "").strip().lower()
    return {
        "pg": "postgres", "postgresql": "postgres", "psql": "postgres",
        "mongo": "mongodb", "mariadb": "mysql", "tidb": "mysql",
    }.get(text, text)


def dsn(ds) -> str:
    """连接串只从环境变量取 —— dot.toml 里不存密钥。"""
    env_name = getattr(ds, "dsn_env", "") or ""
    if not env_name:
        raise RuntimeError(
            f"数据源 {ds.name} 没配 dsn_env。在 [ontology.datasources.{ds.name}] "
            f"里写 dsn_env = \"DOT_XXX_DSN\",再把连接串 export 到那个变量。"
        )
    value = os.environ.get(env_name, "")
    if not value:
        raise RuntimeError(
            f"环境变量 {env_name} 是空的(数据源 {ds.name})。"
            f"export {env_name}='<连接串>' 再试。"
        )
    return value


def _matches(name: str, include: list[str], exclude: list[str]) -> bool:
    low = name.lower()
    if include and not any(fnmatch.fnmatch(low, p.lower()) for p in include):
        return False
    if exclude and any(fnmatch.fnmatch(low, p.lower()) for p in exclude):
        return False
    return True


def introspect(ds, *, connect=None) -> list[dict]:
    """采一个数据源的元数据,返回归一化后的表/集合列表。

    connect 是给测试用的注入口:传一个假的连接工厂就能不碰真库。
    """
    kind = normalize_kind(getattr(ds, "kind", "postgres"))
    if kind == "postgres":
        return _raw_postgres(ds, connect)
    if kind == "mysql":
        return _raw_mysql(ds, connect)
    if kind == "mongodb":
        return _raw_mongo(ds, connect)
    raise RuntimeError(f"不支持的库类型 {kind!r}(只认 postgres / mysql / mongodb)")


def _raw_postgres(ds, connect=None) -> list[dict]:
    if connect is None:
        try:
            import psycopg                      # psycopg 3
        except ImportError:
            try:
                import psycopg2 as psycopg      # 退到 2
            except ImportError:
                raise RuntimeError(
                    "采 postgres 元数据要 psycopg(3 或 2):pip install 'psycopg[binary]'"
                )
        connect = psycopg.connect
    schema = getattr(ds, "schema", "public") or "public"
    with connect(dsn(ds)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = %s AND table_type = 'BASE TABLE'"
            " ORDER BY table_name",
            (schema,),
        )
        tables = [r[0] for r in cur.fetchall()]
        out: list[dict] = []
        for table in tables:
            if not _matches(table, list(ds.include or []), list(ds.exclude or [])):
                continue
            cur.execute(
                "SELECT column_name, data_type, is_nullable, column_default"
                " FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table),
            )
            columns = [
                {"name": r[0], "type": r[1], "nullable": r[2] == "YES", "default": r[3]}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc"
                " JOIN information_schema.key_column_usage kcu"
                "   ON tc.constraint_name = kcu.constraint_name"
                "  AND tc.table_schema = kcu.table_schema"
                " WHERE tc.constraint_type = 'PRIMARY KEY'"
                "   AND tc.table_schema = %s AND tc.table_name = %s"
                " ORDER BY kcu.ordinal_position",
                (schema, table),
            )
            pk = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT kcu.column_name, ccu.table_name, ccu.column_name"
                " FROM information_schema.table_constraints tc"
                " JOIN information_schema.key_column_usage kcu"
                "   ON tc.constraint_name = kcu.constraint_name"
                " JOIN information_schema.constraint_column_usage ccu"
                "   ON tc.constraint_name = ccu.constraint_name"
                " WHERE tc.constraint_type = 'FOREIGN KEY'"
                "   AND tc.table_schema = %s AND tc.table_name = %s",
                (schema, table),
            )
            fks = [{"field": r[0], "table": r[1], "target_field": r[2]}
                   for r in cur.fetchall()]
            out.append({
                "table": table, "schema": schema, "columns": columns,
                "primary_key": pk, "foreign_keys": fks,
            })
    return out


def _raw_mysql(ds, connect=None) -> list[dict]:
    if connect is None:
        try:
            import pymysql
        except ImportError:
            raise RuntimeError("采 mysql 元数据要 pymysql:pip install pymysql")
        connect = pymysql.connect
    schema = getattr(ds, "database", "") or getattr(ds, "schema", "")
    if not schema:
        raise RuntimeError(f"数据源 {ds.name} 没配 database(MySQL 用库名当 schema)")
    with connect(dsn(ds)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
            (schema,),
        )
        out: list[dict] = []
        for (table,) in cur.fetchall():
            if not _matches(table, list(ds.include or []), list(ds.exclude or [])):
                continue
            cur.execute(
                "SELECT column_name, column_type, is_nullable, column_default, column_key"
                " FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table),
            )
            rows = cur.fetchall()
            columns = [
                {"name": r[0], "type": r[1], "nullable": r[2] == "YES", "default": r[3]}
                for r in rows
            ]
            pk = [r[0] for r in rows if r[4] == "PRI"]
            # MySQL 的 FK 要走 KEY_COLUMN_USAGE,顺手拿全库一次比逐表查省事
            cur.execute(
                "SELECT column_name, referenced_table_name, referenced_column_name"
                " FROM information_schema.key_column_usage"
                " WHERE table_schema = %s AND table_name = %s"
                "   AND referenced_table_name IS NOT NULL",
                (schema, table),
            )
            fks = [{"field": r[0], "table": r[1], "target_field": r[2]}
                   for r in cur.fetchall()]
            out.append({
                "table": table, "schema": schema, "columns": columns,
                "primary_key": pk, "foreign_keys": fks,
            })
    return out


def _raw_mongo(ds, connect=None) -> list[dict]:
    if connect is None:
        try:
            from pymongo import MongoClient
        except ImportError:
            raise RuntimeError("采 mongodb 元数据要 pymongo:pip install pymongo")
        connect = MongoClient
    client = connect(dsn(ds), serverSelectionTimeoutMS=8000)
    db_name = getattr(ds, "database", "")
    db = client[db_name] if db_name else client.get_default_database()
    out: list[dict] = []
    for name in sorted(db.list_collection_names()):
        if name.startswith("system."):
            continue   # 系统集合,不是业务数据
        if not _matches(name, list(ds.include or []), list(ds.exclude or [])):
            continue
        sample_size = int(getattr(ds, "sample_size", 50) or 50)
        docs = list(db[name].aggregate([{"$sample": {"size": sample_size}}])) \
            if sample_size else []
        columns = _infer_mongo_fields(docs)
        out.append({
            "table": name, "schema": db_name, "columns": columns,
            "primary_key": ["_id"], "foreign_keys": [],
        })
    client.close()
    return out


def _infer_mongo_fields(docs: list[dict]) -> list[dict]:
    """采样文档 -> 字段表。低基数字符串会被当成枚举,这是 mongo 最值钱的信息。"""
    seen: dict[str, dict] = {}
    for doc in docs:
        for key, value in (doc or {}).items():
            slot = seen.setdefault(key, {"name": key, "types": set(), "values": set()})
            slot["types"].add(_mongo_type(value))
            if isinstance(value, str) and len(slot["values"]) <= 12:
                slot["values"].add(value)
    columns: list[dict] = []
    for key, slot in seen.items():
        types = sorted(slot["types"])
        kind = types[0] if len(types) == 1 else "mixed"
        values = sorted(slot["values"])
        enum = values if kind == "string" and 1 < len(values) <= 12 else []
        columns.append({
            "name": key, "type": kind, "nullable": True, "default": None,
            "enum": enum,
        })
    columns.sort(key=lambda c: (c["name"] != "_id", c["name"]))
    return columns


def _mongo_type(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bytes):
        return "binary"
    return "string"


# --------------------------------------------------------------- 归一化/合并

def _snake_words(name: str) -> list[str]:
    parts: list[str] = []
    for chunk in str(name).split("_"):
        parts.extend(m.group(0) for m in _SNAKE.finditer(chunk) if m.group(0))
    return [p.lower() for p in parts if p]


def suggest_label(name: str) -> str:
    """按词表猜一个中文名。猜不到就把原词原样返回 —— 猜测不如诚实。"""
    low = str(name or "").strip().lower()
    if not low:
        return ""
    if low in _LABEL_WORDS:
        return _LABEL_WORDS[low]
    if low.endswith("_id") and low[:-3] in _LABEL_WORDS:
        return _LABEL_WORDS[low[:-3]] + "ID"
    words = _snake_words(low)
    if not words:
        return str(name)
    translated = [_LABEL_WORDS.get(w, w) for w in words]
    if all(any("\u4e00" <= ch <= "\u9fff" for ch in t) for t in translated):
        return "".join(translated)
    # 表名多半是复数:orders -> order
    if len(words) == 1 and words[0].endswith("s") and words[0][:-1] in _LABEL_WORDS:
        return _LABEL_WORDS[words[0][:-1]]
    return "".join(translated) if any(
        any("\u4e00" <= ch <= "\u9fff" for ch in t) for t in translated
    ) else str(name)


def semantic_of(db_type: str, name: str = "") -> str:
    t = str(db_type or "").lower()
    if _ID_NAME.search(str(name or "")) or t in ("uuid",):
        return "id"
    for keys, semantic in _SEMANTIC_RULES:
        if any(k in t for k in keys):
            return semantic
    return "string"


def _entity_id(table: str, ds_name: str, taken: set[str]) -> str:
    base = _SNAKE_SAFE.sub("_", str(table).strip().lower()) or "entity"
    if base not in taken:
        return base
    return f"{ds_name}_{base}"


_SNAKE_SAFE = re.compile(r"[^a-z0-9_]+")


def to_entity(raw: dict, ds, *, taken: set[str]) -> dict:
    """一组归一化元数据 -> 本体库里的实体(草稿态)。纯函数,可单测。"""
    table = raw.get("table") or ""
    eid = _entity_id(table, getattr(ds, "name", "ds"), taken)
    fields: list[dict] = []
    for col in raw.get("columns") or []:
        fname = str(col.get("name") or "").strip()
        if not fname:
            continue
        enum = [str(x) for x in (col.get("enum") or [])]
        fields.append({
            "name": fname,
            "label": suggest_label(fname),
            "type": str(col.get("type") or ""),
            "semantic": "enum" if enum else semantic_of(col.get("type"), fname),
            "nullable": bool(col.get("nullable", True)),
            "default": col.get("default"),
            "description": "",
            "enum": enum,
            "pii": bool(re.search(r"phone|mobile|email|id_card|password|address", fname, re.I)),
            "curated": False,
        })
    relations = []
    for fk in raw.get("foreign_keys") or []:
        relations.append({
            "id": f"{eid}_{fk.get('field')}",
            "kind": "many_to_one",
            "field": fk.get("field"),
            "target": str(fk.get("table") or ""),
            "target_field": fk.get("target_field") or "id",
            "label": f"{suggest_label(fk.get('field'))}关联的{suggest_label(fk.get('table'))}",
        })
    return {
        "id": eid,
        "label": suggest_label(table),
        "description": "",
        "domain": "",
        "owner": "",
        "tags": [],
        "status": "draft",
        "curated": False,
        "source": {
            "datasource": getattr(ds, "name", ""),
            "schema": raw.get("schema") or "",
            "table": table,
        },
        "primary_key": [str(x) for x in (raw.get("primary_key") or [])],
        "fields": fields,
        "relations": relations,
        "missing": False,
    }


def merge(doc: dict, ds_name: str, raws: list[dict], *, ds=None,
          now: str | None = None) -> dict:
    """把采集结果并进本体库。返回一份统计,给 CLI 打印。

    合并策略,有意不对称:
      - 人工改过的实体(curated=true)/字段(curated=true):标注、枚举、关系一律保留,
        只刷新物理类型;新列不自动加进来(会静默改人的设计),记在 stats 里等确认。
      - 没改过的:字段跟着库走。
      - 库里有、本体库里没有的:新建草稿实体。
      - 本体库有、库里没了:标 missing=true,不删 —— 删了人工标注就没了。
    """
    stamp = now or _now()
    by_table = {
        str((e.get("source") or {}).get("table") or ""): e
        for e in doc.get("entities") or []
    }
    by_id = entity_map(doc)
    taken = set(by_id)
    stats: dict = {"added": [], "updated": [], "missing": [], "field_added": [],
                   "conflicts": [], "datasource": ds_name}

    # id 可能撞车(两个库有同名表):新实体按「ds_表名」退让
    for raw in raws:
        table = str(raw.get("table") or "")
        ent = by_table.get(table)
        cols = {str(c.get("name")): c for c in (raw.get("columns") or [])}
        if ent is None:
            fresh = to_entity(raw, ds or _DsStub(ds_name), taken=taken)
            taken.add(fresh["id"])
            doc.setdefault("entities", []).append(fresh)
            stats["added"].append(fresh["id"])
            continue

        current = {str(f.get("name")): f for f in (ent.get("fields") or [])}
        if ent.get("curated"):
            # 人改过这个实体:只补物理类型,别动人家的标注
            for name, fld in current.items():
                if name in cols:
                    fld["type"] = str(cols[name].get("type") or fld.get("type") or "")
            extra = sorted(set(cols) - set(current))
            if extra:
                stats["conflicts"].append(
                    f"{ent['id']}:库里新增字段 {', '.join(extra)},实体已人工编辑,没有自动合并"
                )
        else:
            for name, col in cols.items():
                if name not in current:
                    ent.setdefault("fields", []).append({
                        "name": name,
                        "label": suggest_label(name),
                        "type": str(col.get("type") or ""),
                        "semantic": "enum" if col.get("enum") else semantic_of(
                            col.get("type"), name),
                        "nullable": bool(col.get("nullable", True)),
                        "default": col.get("default"),
                        "description": "",
                        "enum": [str(x) for x in (col.get("enum") or [])],
                        "pii": bool(re.search(
                            r"phone|mobile|email|id_card|password|address", name, re.I)),
                        "curated": False,
                    })
                    stats["field_added"].append(f"{ent['id']}.{name}")
                else:
                    fld = current[name]
                    fld["type"] = str(col.get("type") or fld.get("type") or "")
                    if not fld.get("curated"):
                        fld["nullable"] = bool(col.get("nullable", True))
                        if col.get("enum"):
                            fld["enum"] = [str(x) for x in col["enum"]]
            if not ent.get("primary_key"):
                ent["primary_key"] = [str(x) for x in (raw.get("primary_key") or [])]
            ent["missing"] = False
            stats["updated"].append(ent["id"])

    present = {str(r.get("table") or "") for r in raws}
    for table, ent in by_table.items():
        if table and table not in present and not ent.get("missing"):
            ent["missing"] = True
            stats["missing"].append(str(ent.get("id") or table))

    src = doc.setdefault("sources", {}).setdefault(ds_name, {})
    src.update({
        "kind": normalize_kind(getattr(ds, "kind", "") if ds is not None else src.get("kind", "")),
        "schema": getattr(ds, "schema", "") if ds is not None else src.get("schema", ""),
        "database": getattr(ds, "database", "") if ds is not None else src.get("database", ""),
        "introspected_at": stamp,
        "tables": len(raws),
    })
    _infer_relations(doc)
    return stats


class _DsStub:
    """只为了让 to_entity 拿到 name/kind,给纯合并测试用。"""

    def __init__(self, name: str):
        self.name = name
        self.kind = ""
        self.schema = ""
        self.database = ""


def _infer_relations(doc: dict) -> None:
    """按命名约定补关系:`customer_id` -> 实体 customers / customer。

    真 FK 优先:已经有指向的关系就不动。约定推断只补缺,不乱猜更多。
    """
    ents = doc.get("entities") or []
    by_id = entity_map(doc)
    by_table: dict[str, str] = {}
    for ent in ents:
        table = str((ent.get("source") or {}).get("table") or "").lower()
        if table:
            by_table[table] = str(ent.get("id"))
        by_table[str(ent.get("id")).lower()] = str(ent.get("id"))

    for ent in ents:
        have = {str(r.get("field")) for r in (ent.get("relations") or [])}
        for fld in ent.get("fields") or []:
            fname = str(fld.get("name") or "")
            if not _ID_NAME.search(fname) or fname in have or fname == "id":
                continue
            stem = re.sub(r"_?id$", "", fname, flags=re.I).lower()
            if not stem:
                continue
            target = by_table.get(stem) or by_table.get(stem + "s")
            if not target or target == ent.get("id"):
                continue
            ent.setdefault("relations", []).append({
                "id": f"{ent.get('id')}_{fname}",
                "kind": "many_to_one",
                "field": fname,
                "target": target,
                "target_field": "id",
                "label": f"{suggest_label(fname)}关联的{suggest_label(target)}",
                "inferred": True,
            })


# ------------------------------------------------------------------ 派生层

def _md(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _field_rows(entity: dict) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fld in entity.get("fields") or []:
        enum = "、".join(map(str, fld.get("enum") or [])) or "-"
        rows.append((
            str(fld.get("name")), str(fld.get("label") or ""),
            str(fld.get("type") or ""), "否" if fld.get("nullable") else "是",
            enum,
        ))
    return rows


def _table(rows: list[tuple], headers: tuple[str, ...]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(_md(c) for c in row) + " |")
    return "\n".join(out)


def render_entity_md(doc: dict, entity: dict) -> str:
    source = entity.get("source") or {}
    src = "、".join(
        str(x) for x in (
            f"{source.get('datasource')}/{source.get('schema')}.{source.get('table')}"
            if source.get("table") else "",
        ) if x
    ) or "(手工定义,没有物理表)"
    lines = [
        GENERATED_BANNER, "",
        f"# {entity.get('label') or entity.get('id')}(`{entity.get('id')}`)", "",
        f"- 物理位置:{src}",
        f"- 主键:{', '.join(map(str, entity.get('primary_key') or [])) or '-'}",
        f"- 负责人:{entity.get('owner') or '-'}",
        f"- 状态:{entity.get('status') or 'draft'}"
        + ("(库里已经没有了)" if entity.get("missing") else ""),
        f"- 标签:{'、'.join(entity.get('tags') or []) or '-'}", "",
    ]
    if entity.get("description"):
        lines += [str(entity["description"]), ""]
    lines += ["## 字段", "", _table(_field_rows(entity),
                                    ("字段", "含义", "物理类型", "必填", "枚举")), ""]
    rels = entity.get("relations") or []
    if rels:
        lines += ["## 关系", ""]
        for rel in rels:
            lines.append(
                f"- `{rel.get('field')}` → `{rel.get('target')}.{rel.get('target_field')}`"
                f"({rel.get('kind')}){(' · ' + rel['label']) if rel.get('label') else ''}"
                + ("(按命名约定推断)" if rel.get("inferred") else "")
            )
        lines.append("")
    return "\n".join(lines)


def render_index_md(doc: dict) -> str:
    ents = doc.get("entities") or []
    lines = [
        GENERATED_BANNER, "",
        "# 数据字典", "",
        f"- 实体 {len(ents)} 个 · 枚举 {len(doc.get('enums') or [])} 个"
        f" · 指纹 `{doc.get('digest') or digest(doc)}`",
        f"- 生成时间:{doc.get('updated_at') or '-'}",
        "- 这份文件是派生产物。改本体:`dots ontology edit`,然后 `dots ontology apply`。",
        "",
        "## 实体", "",
        _table(
            [(str(e.get("id")), str(e.get("label")), str(len(e.get("fields") or [])),
              str((e.get("source") or {}).get("table") or "-"),
              str(e.get("status") or "draft"))
             for e in ents],
            ("ID", "名称", "字段数", "物理表", "状态"),
        ), "",
        "## 关系", "",
        "```mermaid", render_mermaid(doc), "```", "",
    ]
    enums = doc.get("enums") or []
    if enums:
        lines += ["## 枚举", ""]
        for en in enums:
            values = "、".join(
                f"`{v.get('value')}`({v.get('label')})" if v.get("label")
                else f"`{v.get('value')}`"
                for v in en.get("values") or []
            )
            lines.append(f"- **{en.get('id')}**:{values}")
        lines.append("")
    terms = doc.get("glossary") or []
    if terms:
        lines += ["## 术语", ""]
        for t in terms:
            lines.append(f"- **{t.get('term')}**:{t.get('definition', '')}")
        lines.append("")
    return "\n".join(lines)


def render_mermaid(doc: dict) -> str:
    lines = ["erDiagram"]
    for ent in doc.get("entities") or []:
        if ent.get("status") == "deprecated":
            continue
        eid = _DDL_UNSAFE.sub("_", str(ent.get("id")))[:40] or "entity"
        for fld in (ent.get("fields") or [])[:12]:
            fname = _DDL_UNSAFE.sub("_", str(fld.get("name"))) or "field"
            ftype = _DDL_UNSAFE.sub("_", str(fld.get("type") or "string"))[:24] or "string"
            key = "PK" if fld.get("name") in (ent.get("primary_key") or []) else ""
            lines.append(f"    {eid} {{ {ftype} {fname} {key} }}".rstrip())
    for ent in doc.get("entities") or []:
        eid = _DDL_UNSAFE.sub("_", str(ent.get("id"))) or "entity"
        for rel in ent.get("relations") or []:
            target = _DDL_UNSAFE.sub("_", str(rel.get("target"))) or "entity"
            arrow = {
                "one_to_many": "||--o{",
                "one_to_one": "||--||",
                "many_to_many": "}o--o{",
            }.get(rel.get("kind"), "}o--||")
            lines.append(f"    {target} {arrow} {eid} : \"{rel.get('field')}\"")
    return "\n".join(lines)


_SQL_TYPES = {
    "id": "BIGINT", "integer": "INTEGER", "decimal": "NUMERIC(18,2)",
    "datetime": "TIMESTAMPTZ", "boolean": "BOOLEAN", "json": "JSONB",
    "binary": "BYTEA", "string": "TEXT", "enum": "TEXT",
}


def render_ddl(doc: dict) -> str:
    lines = [f"-- {GENERATED_BANNER.strip('<!-> ')}", ""]
    for ent in doc.get("entities") or []:
        if ent.get("status") == "deprecated" or ent.get("missing"):
            continue
        table = _DDL_UNSAFE.sub("_", str((ent.get("source") or {}).get("table") or ent.get("id")))
        lines.append(f"-- {ent.get('label') or ent.get('id')}(本体 id: {ent.get('id')})")
        lines.append(f"CREATE TABLE IF NOT EXISTS {table} (")
        cols = []
        for fld in ent.get("fields") or []:
            name = _DDL_UNSAFE.sub("_", str(fld.get("name"))) or "col"
            sql_type = _SQL_TYPES.get(str(fld.get("semantic") or ""), "TEXT")
            null = "" if fld.get("nullable") else " NOT NULL"
            cols.append(f"    {name} {sql_type}{null}")
        pk = [str(x) for x in (ent.get("primary_key") or [])]
        if pk:
            cols.append("    PRIMARY KEY (" + ", ".join(
                _DDL_UNSAFE.sub("_", c) for c in pk) + ")")
        lines.append(",\n".join(cols))
        lines.append(");")
        lines.append("")
    return "\n".join(lines)


_PY_TYPES = {
    "id": "int", "integer": "int", "decimal": "Decimal", "datetime": "datetime",
    "boolean": "bool", "json": "dict", "binary": "bytes", "string": "str",
}


def render_python(doc: dict) -> str:
    lines = [
        f'"""{GENERATED_BANNER.strip("<!-> ")}',
        "",
        "实体模型。字段名和本体库一致,改动请改 ontology.json 后重新生成。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass, field",
        "from datetime import datetime",
        "from decimal import Decimal",
        "",
        "",
    ]
    enums = doc.get("enums") or []
    for en in enums:
        name = _py_class(str(en.get("id")))
        lines.append(f"class {name}(str):")
        values = en.get("values") or []
        if not values:
            lines.append("    pass")
        for v in values:
            lines.append(f'    {_py_const(str(v.get("value")))} = "{v.get("value")}"')
        lines += ["", ""]

    for ent in doc.get("entities") or []:
        if ent.get("status") == "deprecated":
            continue
        cls = _py_class(str(ent.get("id")))
        lines.append("@dataclass")
        lines.append(f"class {cls}:")
        desc = str(ent.get("description") or ent.get("label") or "")
        if desc:
            lines.append(f'    """{desc}"""')
        fields = ent.get("fields") or []
        if not fields:
            lines.append("    pass")
        for fld in fields:
            py_type = _PY_TYPES.get(str(fld.get("semantic") or ""), "str")
            if fld.get("nullable") or fld.get("name") not in (ent.get("primary_key") or []):
                py_type = f"{py_type} | None"
            default = " = None"
            if fld.get("name") in (ent.get("primary_key") or []):
                default = ""
            label = f"  # {fld.get('label')}" if fld.get("label") else ""
            enum = f"  # 枚举:{'、'.join(map(str, fld.get('enum') or []))}" if fld.get("enum") else ""
            lines.append(f"    {_py_attr(str(fld.get('name')))}: {py_type}{default}{label}{enum}")
        lines += ["", ""]
    return "\n".join(lines)


def _py_class(name: str) -> str:
    words = _snake_words(name) or ["entity"]
    cls = "".join(w.capitalize() for w in words)
    return "E" + cls if cls[0].isdigit() else cls


def _py_attr(name: str) -> str:
    """物理列名不一定能当 Python 标识符(2fa_code / 中文列名)。"""
    text = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_") or "col"
    return "_" + text if text[0].isdigit() else text


def _py_const(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
    if not text:
        return "VALUE"
    return "V_" + text if text[0].isdigit() else text


_TS_TYPES = {
    "id": "string", "integer": "number", "decimal": "number", "datetime": "string",
    "boolean": "boolean", "json": "Record<string, unknown>", "binary": "Uint8Array",
    "string": "string",
}


def render_typescript(doc: dict) -> str:
    lines = [
        f"// {GENERATED_BANNER.strip('<!-> ')}",
        "// 字段名和本体库一致;改本体后重新生成,别在这里手改。",
        "",
    ]
    for ent in doc.get("entities") or []:
        if ent.get("status") == "deprecated":
            continue
        if ent.get("description"):
            lines.append(f"/** {ent['description']} */")
        lines.append(f"export interface {_py_class(str(ent.get('id')))} {{")
        for fld in ent.get("fields") or []:
            ts_type = _TS_TYPES.get(str(fld.get("semantic") or ""), "string")
            if fld.get("enum"):
                ts_type = " | ".join(f'"{v}"' for v in fld["enum"]) or ts_type
            opt = "?" if fld.get("nullable") else ""
            label = f"  // {fld.get('label')}" if fld.get("label") else ""
            lines.append(f"  {fld.get('name')}{opt}: {ts_type};{label}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def derived_files(doc: dict) -> dict[str, str]:
    """派生文件全量。键是相对 generated/ 的 POSIX 路径。"""
    files = {
        "README.md": render_index_md(doc),
        "schema.sql": render_ddl(doc),
        "models.py": render_python(doc),
        "types.ts": render_typescript(doc),
        "er.mmd": render_mermaid(doc) + "\n",
    }
    for ent in doc.get("entities") or []:
        eid = _DDL_UNSAFE.sub("_", str(ent.get("id"))) or "entity"
        files[f"entities/{eid}.md"] = render_entity_md(doc, ent)
    return files


def derived_dir(cfg) -> Path:
    return cfg.workspace / cfg.ontology.derived


def manifest_path(cfg) -> Path:
    return derived_dir(cfg) / "manifest.json"


def read_manifest(cfg) -> dict:
    p = manifest_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(cfg, doc: dict | None = None) -> dict:
    """重写全部派生文件,并删掉上次有、这次没有的(实体被删的情况)。"""
    doc = doc if doc is not None else load(cfg)
    problems = validate(doc)
    if problems:
        raise SystemExit("本体库校验不过,没有生成任何东西:\n  - " + "\n  - ".join(problems))

    root = derived_dir(cfg)
    files = derived_files(doc)
    old = read_manifest(cfg).get("files") or {}
    written: list[str] = []
    removed: list[str] = []

    for rel, content in files.items():
        target = root / rel
        old_text = target.read_text(encoding="utf-8") if target.exists() else None
        if old_text != content:
            _atomic_write(target, content)
            written.append(rel)
    for rel in old:
        if rel not in files:
            stale = root / rel
            if stale.exists():
                stale.unlink()
                removed.append(rel)

    manifest = {
        "digest": digest(doc),
        "updated_at": doc.get("updated_at") or _now(),
        "files": {rel: hashlib.sha1(text.encode("utf-8")).hexdigest()
                  for rel, text in files.items()},
    }
    _atomic_write(manifest_path(cfg), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"written": written, "removed": removed, "digest": manifest["digest"],
            "files": sorted(files)}


def status(cfg, doc: dict | None = None) -> dict:
    """派生层是不是还跟本体库一致。改本体后忘了 apply,这里会红。"""
    doc = doc if doc is not None else load(cfg)
    man = read_manifest(cfg)
    if not man:
        return {"synced": False, "reason": "还没生成过", "changed": [], "missing": []}
    want = digest(doc)
    root = derived_dir(cfg)
    expected = {rel: hashlib.sha1(text.encode("utf-8")).hexdigest()
                for rel, text in derived_files(doc).items()}
    changed = [rel for rel, h in expected.items()
               if (man.get("files") or {}).get(rel) != h]
    missing = [rel for rel in expected if not (root / rel).exists()]
    manual = [rel for rel, h in (man.get("files") or {}).items()
              if (root / rel).exists()
              and hashlib.sha1((root / rel).read_bytes()).hexdigest() != h]
    synced = not changed and not missing
    reason = "" if synced else (
        "本体库改过,还没 apply" if man.get("digest") != want else "派生文件被手改过"
    )
    return {
        "synced": synced, "reason": reason, "digest": want,
        "manifest_digest": man.get("digest", ""),
        "changed": sorted(changed), "missing": sorted(missing),
        "hand_edited": sorted(manual),
    }


def demo() -> None:
    """不碰数据库、不联网的自检:纯手工本体 -> 校验 -> 派生 -> 手改保留。"""
    doc = empty()
    doc["entities"] = [
        {
            "id": "customer", "label": "客户", "description": "",
            "status": "confirmed", "curated": True,
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
                {"name": "customer_id", "label": "客户", "type": "bigint",
                 "semantic": "id"},
                {"name": "status", "label": "状态", "type": "text", "semantic": "enum",
                 "enum": ["paid", "unpaid"]},
            ],
            "relations": [],
        },
    ]
    doc["enums"] = [{"id": "order_status", "values": [
        {"value": "paid", "label": "已支付"}, {"value": "unpaid", "label": "未支付"}]}]

    assert validate(doc) == []
    broken = json.loads(json.dumps(doc))
    broken["entities"][1]["relations"] = [{"target": "ghost", "kind": "many_to_one"}]
    assert any("ghost" in p for p in validate(broken))
    assert validate({"entities": [{"id": "a", "label": "A", "fields": [
        {"name": "x"}, {"name": "x"}]}]}), "重复字段名应该被拒"

    assert find_entity(doc, "订单") is doc["entities"][1]
    assert find_entity(doc, "orders") is doc["entities"][1]
    assert suggest_label("created_at") == "创建时间"
    assert semantic_of("numeric(18,2)", "amount") == "decimal"
    assert semantic_of("bigint", "customer_id") == "id"

    _infer_relations(doc)
    assert doc["entities"][1]["relations"], "customer_id 应该推断出关系"
    assert doc["entities"][1]["relations"][0]["target"] == "customer"

    files = derived_files(doc)
    assert "entities/orders.md" in files
    assert "customer_id" in files["schema.sql"]
    assert "class Orders" in files["models.py"]
    assert "export interface Orders" in files["types.ts"]
    assert "orders" in files["README.md"]

    # 手改过的实体在重新合并时不能被库里的元数据盖掉
    raw = [{
        "table": "customers", "schema": "public",
        "columns": [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "name", "type": "varchar", "nullable": False},
            {"name": "vip_level", "type": "int", "nullable": True},
        ],
        "primary_key": ["id"], "foreign_keys": [],
    }]
    stats = merge(doc, "crm", raw, ds=_DsStub("crm"))
    assert stats["conflicts"], "curated 实体应报冲突而不是静默改"
    assert "vip_level" not in {f["name"] for f in doc["entities"][0]["fields"]}
    print("ontology: ok")


if __name__ == "__main__":
    demo()
