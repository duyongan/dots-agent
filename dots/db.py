"""所有 SQL 都在这里。别的模块一个字都别写 SQL。

四张表 + 一张 meta:
  goals      目标。没有它就没有「持续」——进程重启后靠它接着干。
  events     外部事件。UNIQUE(source, key) 就是幂等,重复扔同一个文件不会跑两次。
  approvals  挂起的动作。token 是主键,你回邮件时靠主题里的 token 对上号。
  audit      活动视图的唯一数据源,也是成本统计的唯一数据源。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS goals (
    id           TEXT PRIMARY KEY,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    origin       TEXT NOT NULL DEFAULT 'cli',
    parent_id    TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    budget       INTEGER,
    tokens       INTEGER NOT NULL DEFAULT 0,
    summary      TEXT,
    evidence     TEXT,
    bot          TEXT NOT NULL DEFAULT 'dot',
    handoffs     INTEGER NOT NULL DEFAULT 0,
    handoff_from TEXT,
    handoff_note TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    seen_at     REAL NOT NULL,
    goal_id     TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    UNIQUE(source, key)
);

CREATE TABLE IF NOT EXISTS approvals (
    token       TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    decided_at  REAL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    goal_id     TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    tokens      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_audit_goal ON audit(goal_id);
"""

# 目标状态机
PENDING = "pending"           # 建了但没批准(agent 自己派生的走这里)
READY = "ready"               # 可以跑
RUNNING = "running"           # 正在跑
INTERRUPTED = "interrupted"   # 挂了,等审批
REVIEW = "pending_review"     # agent 说 done 但没给证据
DONE = "done"
FAILED = "failed"

TERMINAL = (DONE, FAILED)
ACTIVE = (READY, RUNNING, INTERRUPTED)


def now() -> float:
    return time.time()


def new_id() -> str:
    return secrets.token_hex(3)


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# v1 -> v2:bot 列(多 agent 交接)。新增列对老库必须走 ALTER,不能重跑 DDL。
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE goals ADD COLUMN bot TEXT NOT NULL DEFAULT 'dot'",
        "ALTER TABLE goals ADD COLUMN handoffs INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE goals ADD COLUMN handoff_from TEXT",
        "ALTER TABLE goals ADD COLUMN handoff_note TEXT",
    ],
}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    current = int(row["v"]) if row else SCHEMA_VERSION
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for stmt in MIGRATIONS.get(version, []):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass   # 列已存在 —— 半途中断过,重跑无害
        conn.execute(
            "UPDATE meta SET v = ? WHERE k = 'schema_version'", (str(version),)
        )
    conn.commit()


# ---------------------------------------------------------------- meta / 游标

def meta_get(conn, k: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))
    conn.commit()


def cursor_get(conn, source: str) -> str:
    return meta_get(conn, f"cursor:{source}", "") or ""


def cursor_set(conn, source: str, value) -> None:
    meta_set(conn, f"cursor:{source}", value)


# ---------------------------------------------------------------------- goals

def add_goal(conn, text: str, *, origin: str = "cli", status: str = PENDING,
             parent_id: str | None = None, budget: int | None = None,
             bot: str = "dot") -> str:
    gid = new_id()
    t = now()
    conn.execute(
        "INSERT INTO goals(id, text, status, origin, parent_id, created_at,"
        " updated_at, budget, bot) VALUES(?,?,?,?,?,?,?,?,?)",
        (gid, text, status, origin, parent_id, t, t, budget, bot),
    )
    conn.commit()
    return gid


def handoff_goal(conn, gid: str, target_bot: str, from_bot: str, note: str) -> bool:
    """把目标交给另一个 bot。

    用 WHERE 带上 from_bot,这样两个 bot 同时想交出去时只有一个生效 ——
    和 claim_goal 是同一个套路。handoffs 计数在这里 +1,防乒乓就靠它。
    """
    cur = conn.execute(
        "UPDATE goals SET bot = ?, handoff_from = ?, handoff_note = ?,"
        " handoffs = handoffs + 1, status = ?, updated_at = ?"
        " WHERE id = ? AND bot = ?",
        (target_bot, from_bot, note, READY, now(), gid, from_bot),
    )
    conn.commit()
    return cur.rowcount > 0


def set_goal(conn, gid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE goals SET {cols}, updated_at = ? WHERE id = ?",
        (*fields.values(), now(), gid),
    )
    conn.commit()


def get_goal(conn, gid: str):
    return conn.execute("SELECT * FROM goals WHERE id = ?", (gid,)).fetchone()


def list_goals(conn, status: str | None = None, limit: int = 50):
    if status:
        return conn.execute(
            "SELECT * FROM goals WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM goals ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()


def pick_ready(conn):
    """一次只取一个。单机单 worker,不需要抢锁。"""
    return conn.execute(
        "SELECT * FROM goals WHERE status = ? ORDER BY created_at LIMIT 1", (READY,)
    ).fetchone()


def claim_goal(conn, gid: str, from_status: str) -> bool:
    """原子地抢一个目标。返回 True 才算抢到。

    WHERE 里带 status 是关键:并发的两个进程里只有一个能把
    ready 改成 running,另一个 rowcount=0 自动退出,不需要额外的锁。
    """
    cur = conn.execute(
        "UPDATE goals SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (RUNNING, now(), gid, from_status),
    )
    conn.commit()
    return cur.rowcount > 0


def stale_running(conn, older_than_ts: float):
    """被 kill 的进程留在 running 状态的目标。checkpoint 还在,放回 ready 能接着跑。"""
    return conn.execute(
        "SELECT * FROM goals WHERE status = ? AND updated_at < ?", (RUNNING, older_than_ts)
    ).fetchall()


def add_tokens(conn, gid: str, n: int) -> None:
    if n:
        conn.execute(
            "UPDATE goals SET tokens = tokens + ?, updated_at = ? WHERE id = ?",
            (n, now(), gid),
        )
        conn.commit()


# --------------------------------------------------------------------- events

def add_event(conn, source: str, key: str, payload: dict, goal_id: str | None = None,
              status: str = "new") -> bool:
    """返回 True 表示这是新事件。UNIQUE 冲突 = 见过 = 返回 False。

    status="self" 用来预先占掉 agent 自己写出来的文件的 key,见 worker._suppress_self_writes。
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO events(source, key, payload, seen_at, goal_id, status)"
        " VALUES(?,?,?,?,?,?)",
        (source, key, json.dumps(payload, ensure_ascii=False), now(), goal_id, status),
    )
    conn.commit()
    return cur.rowcount > 0


def new_events(conn):
    return conn.execute(
        "SELECT * FROM events WHERE status = 'new' ORDER BY id"
    ).fetchall()


def consume_event(conn, eid: int, goal_id: str | None, status: str = "consumed") -> None:
    conn.execute(
        "UPDATE events SET status = ?, goal_id = ? WHERE id = ?", (status, goal_id, eid)
    )
    conn.commit()


# ------------------------------------------------------------------ approvals

def create_approval(conn, token: str, goal_id: str, tool: str, args: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO approvals(token, goal_id, tool, args, status, created_at)"
        " VALUES(?,?,?,?,'pending',?)",
        (token, goal_id, tool, json.dumps(args, ensure_ascii=False), now()),
    )
    conn.commit()


def get_approval(conn, token: str):
    return conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()


def pending_approvals(conn, goal_id: str | None = None):
    if goal_id:
        return conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' AND goal_id = ?"
            " ORDER BY created_at", (goal_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"
    ).fetchall()


def last_decided_approval(conn, goal_id: str):
    """续跑时用它取出 resume 值。没批准的返回 None。"""
    return conn.execute(
        "SELECT * FROM approvals WHERE goal_id = ? AND status != 'pending'"
        " ORDER BY decided_at DESC LIMIT 1",
        (goal_id,),
    ).fetchone()


def expire_approvals(conn, cutoff_ts: float) -> int:
    """超时未批的挂起动作一律判为拒绝 —— 失败要往安全的方向倒。

    一个永远等不到回复的审批 = 一个目标永远卡住,而你还以为它在跑。
    """
    cur = conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ?"
        " WHERE status = 'pending' AND created_at < ?",
        (now(), cutoff_ts),
    )
    conn.commit()
    return cur.rowcount


def resolve_approval(conn, token: str, decision: str) -> bool:
    """decision: approved | rejected。只认第一次,重复回复无效。"""
    cur = conn.execute(
        "UPDATE approvals SET status = ?, decided_at = ? WHERE token = ? AND status = 'pending'",
        (decision, now(), token),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------- audit

def audit(conn, goal_id: str | None, kind: str, detail: str = "", tokens: int = 0) -> None:
    conn.execute(
        "INSERT INTO audit(ts, goal_id, kind, detail, tokens) VALUES(?,?,?,?,?)",
        (now(), goal_id, kind, detail, tokens),
    )
    conn.commit()


def recent_audit(conn, limit: int = 50, goal_id: str | None = None):
    if goal_id:
        return conn.execute(
            "SELECT * FROM audit WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
            (goal_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def tokens_since(conn, ts: float) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) AS n FROM audit WHERE ts >= ?", (ts,)
    ).fetchone()
    return int(row["n"])
