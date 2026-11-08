"""全流程流水线:需求分析 → 原型设计 → 开发 → 测试 → 修复 → 归档。

形状照的是「spec 驱动研发」那一套:每条流水线一个目录,四个阶段的产物分开放
(PRD / design / dev / test),阶段之间靠产物文件和 Jira 单号绑定,不靠谁记性好。
和那套的区别只有一处,是有意的:阶段推进由 worker 在目标真正完成之后自动做,
所以进程重启、断点续跑都不会把一条流水线卡在半路。

目录长这样(相对 workspace):

    projects/<slug>/
      pipeline.json          状态机,人可以看也可以改
      PRD/requirements.md    需求文档(必须含验收标准)
      PRD/questions.md       需求澄清问题清单(带选项,人点选)
      design/prototype.md    原型说明
      design/prototype.html  能点的静态原型
      dev/spec.md            技术方案 + 任务拆分
      dev/tasks/T01-*.md     最小可执行任务(一个任务一个文件)
      dev/src/               代码
      test/report.md         测试报告,末行必须是 VERDICT: PASS 或 FAIL
      fix/fix-log.md         修复记录
      archive/summary.md     版本归档:功能点 + 上下文,给以后排查用

阶段推进的判据是**产物文件真的在**,不是模型说完成了。产物没落盘就重派一次,
两次还不行就停在那儿并记 audit —— 悄悄往前推比卡住更糟。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dots import db
from dots import ontology as ont

# 阶段的固定信息。artifacts 是推进的门槛,check 是「文档里必须出现的小节」,
# 缺小节也算没做完 —— 否则下一阶段拿到的是空壳。
STAGES: dict[str, dict] = {
    "requirements": {
        "title": "需求分析",
        "bot": "pm",
        "dir": "PRD",
        "artifacts": ["PRD/requirements.md", "PRD/questions.md"],
        "check": {
            "PRD/requirements.md": ["## 背景", "## 用户故事", "## 验收标准",
                                    "## 非目标", "## 待确认问题"],
            "PRD/questions.md": ["## 问题"],
        },
    },
    "prototype": {
        "title": "原型设计",
        "bot": "designer",
        "dir": "design",
        "artifacts": ["design/prototype.md", "design/prototype.html"],
        "check": {
            "design/prototype.md": ["## 页面", "## 交互", "## 状态"],
        },
    },
    "dev": {
        "title": "开发",
        "bot": "dev",
        "dir": "dev",
        "artifacts": ["dev/spec.md", "dev/notes.md"],
        "check": {
            "dev/spec.md": ["## 方案", "## 任务拆分"],
            "dev/notes.md": ["## 怎么跑"],
        },
    },
    "test": {
        "title": "测试",
        "bot": "qa",
        "dir": "test",
        "artifacts": ["test/report.md"],
        "check": {
            "test/report.md": ["VERDICT:"],
        },
    },
    "fix": {
        "title": "修复",
        "bot": "fixer",
        "dir": "fix",
        "artifacts": ["fix/fix-log.md"],
        "check": {
            "fix/fix-log.md": ["## 缺陷", "## 根因", "## 改动", "## 回归"],
        },
    },
    "archive": {
        "title": "归档",
        "bot": "pm",
        "dir": "archive",
        "artifacts": ["archive/summary.md"],
        "check": {
            "archive/summary.md": ["## 功能点", "## 产物", "## 上下文"],
        },
    },
}

TRACKS: dict[str, list[str]] = {
    # 新需求:从需求分析一路走到底。修复阶段列在这里,是为了测试不过时
    # 有地方可去,也让「读前序产物」能看见修复记录。
    "feature": ["requirements", "prototype", "dev", "test", "fix", "archive"],
    # BUG:直接从修复开始,修完再测;测过就归档
    "bugfix": ["fix", "test", "archive"],
}

_MARKER = re.compile(r"【SDLC\s+([0-9a-f]{6})\s+(\w+)\s+(\d+)/(\d+)】")
_BEGIN = "<!-- ontology:begin -->"
_END = "<!-- ontology:end -->"


# ------------------------------------------------------------------ 目录与读写

def new_id() -> str:
    return secrets.token_hex(3)


def slugify(text: str, *, limit: int = 32) -> str:
    """中文需求也得能当目录名 —— 保留中文,去掉路径分隔符和空白。"""
    raw = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(text or "").strip())
    raw = re.sub(r"-{2,}", "-", raw).strip("-").lower()
    return raw[:limit] or "pipeline"


def directory(cfg) -> Path:
    return cfg.workspace / cfg.sdlc.dir


def project_dir(cfg, p: dict) -> Path:
    return directory(cfg) / p["slug"]


def pipeline_path(cfg, p: dict) -> Path:
    return project_dir(cfg, p) / "pipeline.json"


def load(cfg, key: str) -> dict | None:
    """按 id 或 slug 找一条流水线。找不到返回 None。"""
    root = directory(cfg)
    if not root.exists():
        return None
    key = (key or "").strip()
    direct = root / key / "pipeline.json"
    candidates = [direct] if direct.exists() else sorted(root.glob("*/pipeline.json"))
    for path in candidates:
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if p.get("id") == key or p.get("slug") == key:
            p.setdefault("slug", path.parent.name)
            return p
    return None


def save(cfg, p: dict) -> Path:
    p["updated_at"] = _now()
    path = pipeline_path(cfg, p)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(p, ensure_ascii=False, indent=2) + "\n")
    return path


def list_pipelines(cfg, status: str | None = None) -> list[dict]:
    root = directory(cfg)
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob("*/pipeline.json")):
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        p.setdefault("slug", path.parent.name)
        if status and p.get("status") != status:
            continue
        out.append(p)
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


def _atomic_write(p: Path, text: str) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace(src, dst) -> None:
    """Windows 上 os.replace 偶尔被占用方(杀毒/索引)挡住,重试几次再放弃。

    流水线状态机每推进一个阶段都会重写 pipeline.json,被挡一次整条流水线就断了,
    这里的重试比让调用方去处理 WinError 5 便宜得多。
    """
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _append_history(p: dict, event: str, **extra) -> None:
    p.setdefault("history", []).append({"at": _now(), "event": event, **extra})
    del p["history"][:-80]        # 只留最近 80 条,别让 pipeline.json 无限长


# ------------------------------------------------------------------ 建流水线

def create(cfg, conn, text: str, *, title: str = "", track: str = "feature",
           external: dict | None = None, bug_ids: list[str] | None = None,
           slug: str | None = None) -> dict:
    """建一条流水线,并把第一个阶段的目标排进队列。"""
    if not cfg.sdlc.enabled:
        raise SystemExit("流水线被关掉了([sdlc] enabled = false)。")
    track = (track or "feature").strip().lower()
    if track not in TRACKS:
        raise SystemExit(f"没有 {track!r} 这种流水线。只认:{', '.join(TRACKS)}")
    stages = TRACKS[track]
    text = (text or "").strip()
    if not text:
        raise SystemExit("需求是空的,没东西可做。")

    pid = new_id()
    base = slug or slugify(title or text)
    slug = base
    n = 2
    while (directory(cfg) / slug).exists():     # 同名就加后缀,不覆盖别人的流水线
        slug = f"{base}-{n}"
        n += 1
    p = {
        "id": pid,
        "slug": slug,
        "title": (title or text.splitlines()[0])[:120],
        "requirement": text,
        "track": track,
        "status": "active",
        "stage": stages[0],
        "created_at": _now(),
        "updated_at": _now(),
        "ontology_digest": "",
        "external": dict(external or {}),
        "bug_ids": list(bug_ids or []),
        "rounds": {"fix": 0, "test": 0},
        "stages": {
            s: {"status": "pending", "attempts": 0, "goal_id": None,
                "started_at": None, "finished_at": None}
            for s in stages
        },
        "history": [],
    }
    project_dir(cfg, p).mkdir(parents=True, exist_ok=True)
    save(cfg, p)
    _start_stage(cfg, conn, p, p["stage"])
    return p


def _start_stage(cfg, conn, p: dict, stage: str, *, note: str = "") -> str:
    rec = p["stages"].setdefault(
        stage, {"status": "pending", "attempts": 0, "goal_id": None})
    rec["status"] = "active"
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    rec["started_at"] = rec.get("started_at") or _now()
    bot = STAGES[stage]["bot"]
    gid = db.add_goal(
        conn, goal_text(cfg, p, stage, note=note), origin=f"sdlc:{p['id']}",
        status=db.READY, bot=bot,
    )
    rec["goal_id"] = gid
    rec["status"] = "running"
    _append_history(p, "stage_start", stage=stage, goal_id=gid, attempt=rec["attempts"])
    save(cfg, p)
    db.audit(conn, gid, "sdlc_stage", f"{p['id']} {stage} 第 {rec['attempts']} 次")
    return gid


# ------------------------------------------------------------------ 目标文本

def goal_text(cfg, p: dict, stage: str, *, note: str = "") -> str:
    """一个阶段跑一轮要看的全部东西。写全,别让 agent 猜。"""
    spec = STAGES[stage]
    stages = TRACKS[p["track"]]
    # fix 在 feature 轨迹里是「按需进入」的,index 取不到就用总长度兜着,
    # 别让一个显示用的序号把整条流水线搞崩。
    idx = (stages.index(stage) + 1) if stage in stages else len(stages)
    root = f"{cfg.sdlc.dir}/{p['slug']}"
    lines = [
        f"【SDLC {p['id']} {stage} {idx}/{len(stages)}】",
        f"流水线:{p['title']}({p['track']},目录 {root}/)",
        f"当前阶段:{spec['title']}({stage})",
        "",
        "## 需求原文",
        p["requirement"].strip(),
        "",
    ]
    if p.get("bug_ids"):
        lines += ["## 关联缺陷", ", ".join(p["bug_ids"]), ""]

    lines += ["## 本阶段要产出的文件(路径相对工作区)", ""]
    for rel in spec["artifacts"]:
        must = spec.get("check", {}).get(rel)
        extra = f";必须包含小节:{'、'.join(must)}" if must else ""
        lines.append(f"- `{root}/{rel}`{extra}")
    lines.append("")

    prior = _prior_artifacts(cfg, p, stage)
    if prior:
        lines += ["## 前序产物(先读,别重新发明)", ""]
        for rel in prior:
            state = "存在" if (cfg.workspace / rel).exists() else "缺失"
            lines.append(f"- `{rel}`({state})")
        lines.append("")

    lines += _ontology_block(cfg)
    lines += _mcp_block(cfg)
    lines += ["## 验收门槛", "",
              "产物落盘 + 小节齐全,阶段才会自动推进。只说不做不算完成。", ""]

    if stage == "test":
        lines += [
            "## 测试报告的硬要求", "",
            "- 每条用例写:命令、期望、实际,失败时贴真实断言输出",
            "- 报告最后一个非空行必须是 `VERDICT: PASS` 或 `VERDICT: FAIL`",
            "- FAIL 时列出编号缺陷:现象 / 复现步骤 / 期望 / 影响范围", "",
        ]
    if stage == "fix":
        lines += [
            "## 修复的硬要求", "",
            "- 先复现再改。复现不了的,写清楚为什么复现不了,不要猜着改",
            "- 每个缺陷都要有回归测试(改之前红、改之后绿)",
            "- 多个缺陷先按根因归类,同一处根因合并修一次", "",
        ]
    if stage == "archive":
        lines += [
            "## 归档要写给未来的自己", "",
            "- 功能点 → 文件/接口 的对照表",
            "- 怎么跑起来、怎么验证(命令要能直接粘)",
            "- 已知限制和没做的事(这比做了什么更有用)", "",
        ]
    if stage == "dev":
        lines += [
            "## 开发阶段的建议做法", "",
            "- 先在 `dev/spec.md` 里定方案和任务拆分,再把任务写进 `dev/tasks/T01-*.md`",
            "- 一个任务文件 = 一次能做完并自测的最小单元",
            "- 复杂任务交给子 agent 并行做,每个子任务都要自测后再合回来", "",
        ]
    if stage == "requirements":
        lines += [
            "## 需求澄清的做法", "",
            "- 先把不确定的地方列成 `PRD/questions.md` 的问题清单,每题给 2-4 个选项",
            "- 问题清单是给人点选的,不是给人读的:短、具体、有默认选项",
            "- 答案拿不到就先按最保守的假设推进,并把假设写进「待确认问题」", "",
        ]

    if note:
        lines += ["## 上一次没成的原因", note, ""]

    lines += [
        "## 完成时", "",
        "用 `complete_goal` 交差,evidence 写产物路径(以及 Jira/GitLab 单号这类外部 key)。",
        "evidence 不全的完成会进待复核,流水线不会推进 —— 这不是惩罚,是防呆。",
    ]
    return "\n".join(lines)


def _prior_artifacts(cfg, p: dict, stage: str) -> list[str]:
    """前面阶段已经产出的东西。只列真的存在的,别让 agent 去读空气。"""
    stages = TRACKS[p["track"]]
    base = f"{cfg.sdlc.dir}/{p['slug']}"
    out: list[str] = []
    upto = stages.index(stage) if stage in stages else len(stages)
    for s in stages[:upto]:
        for rel in STAGES[s]["artifacts"]:
            if s == "dev":
                out.append(f"{base}/dev/src/")
            out.append(f"{base}/{rel}")
        rec = p["stages"].get(s) or {}
        if rec.get("status") == "done" and s == "test":
            out.append(f"{base}/fix/fix-log.md")
    # 同一阶段重跑时,顺便让它看一眼上次的产物
    for rel in STAGES[stage]["artifacts"]:
        path = f"{base}/{rel}"
        if (cfg.workspace / path).exists() and path not in out:
            out.append(path)
    seen: set[str] = set()
    uniq = []
    for rel in out:
        if rel not in seen:
            seen.add(rel)
            uniq.append(rel)
    return uniq


def _ontology_block(cfg) -> list[str]:
    """本体库摘要。这是全流程对齐字段名的地方。"""
    if not getattr(cfg, "ontology", None) or not cfg.ontology.enabled:
        return []
    doc = ont.load(cfg)
    text = ont.context(doc, limit=int(getattr(cfg.ontology, "prompt_entities", 40) or 40))
    lines = ["## 本体库(字段名、枚举值以它为准)", ""]
    if text:
        lines += [text, "",
                  "查细节用 `ontology_lookup` 工具。本体库里没有的东西,写进「待确认问题」,",
                  "不要自己造字段名 —— 造出来的名字对不上公司的库,下游全会返工。", ""]
    else:
        lines += ["本体库还是空的。字段名自己定,但要写进「待确认问题」。",
                  "有数据库的话跑 `dots ontology sync` 先把元数据采进来。", ""]
    return lines


def _mcp_block(cfg) -> list[str]:
    """接上的外部系统。只提真正接上的,别让 agent 去调不存在的工具。"""
    if not getattr(cfg, "mcp", None) or not cfg.mcp.enabled:
        return []
    from dots import mcp as mcp_mod

    conns = mcp_mod.connections(cfg)
    if not conns:
        return []
    lines = ["## 外部系统(MCP)", "",
             "这些系统才是单据的正本,工作区里的文件只是过程记录:", ""]
    hints = {
        "jira": "把本阶段的任务或缺陷绑定成一张 Jira 单,单号写进 evidence",
        "feishu": "有需要人拍板/知会的事,发到对应的群",
        "gitlab": "代码改动开分支、提 MR,MR 号写进 evidence",
        "sigma": "需要数据核对时查 Sigma,别自己编指标",
    }
    for name in sorted(conns):
        lines.append(f"- `{name}`:{hints.get(name, '按需使用')}")
    lines += ["", "没有接的系统不要假装调用;工具被规则拒绝就如实上报,不要绕路。", ""]
    return lines


# ------------------------------------------------------------------ 阶段推进

def parse_marker(text: str) -> tuple[str, str, int] | None:
    m = _MARKER.search(text or "")
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def missing_artifacts(cfg, p: dict, stage: str) -> list[str]:
    """产物没落盘 / 缺小节,都算没做完。返回人话清单。"""
    problems = []
    spec = STAGES[stage]
    root = project_dir(cfg, p)
    for rel in spec["artifacts"]:
        path = root / rel
        if not path.exists() or path.stat().st_size == 0:
            problems.append(f"缺产物 {rel}")
            continue
        must = spec.get("check", {}).get(rel) or []
        if not must:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            problems.append(f"{rel} 读不了:{e}")
            continue
        for needle in must:
            if needle not in text:
                problems.append(f"{rel} 缺小节 {needle}")
    return problems


def verdict(report: str) -> str:
    """从测试报告里读结论。只认最后几行里的 VERDICT,别被正文里的 PASS 骗了。"""
    tail = "\n".join((report or "").strip().splitlines()[-8:]).upper()
    if "VERDICT: FAIL" in tail or "VERDICT:FAIL" in tail:
        return "FAIL"
    if "VERDICT: PASS" in tail or "VERDICT:PASS" in tail:
        return "PASS"
    return "UNKNOWN"


def advance_manual(cfg, conn, p: dict, *, note: str = "人工跳过") -> str:
    """人工把当前阶段按「已完成」处理,跳到下一阶段。

    只在 agent 卡住、或者产物在别处已经做好了的时候用。不做产物检查 ——
    这是人的判断,代码不该替他否决,但要留痕。
    """
    stage = p.get("stage") or ""
    if p.get("status") == "done":
        return f"{p['id']} 已经完成了,不用再推。"
    rec = p["stages"].setdefault(stage, {})
    rec["status"] = "done"
    rec["finished_at"] = _now()
    _append_history(p, "stage_forced", stage=stage, note=note)
    nxt, why = _next_stage(cfg, p, stage)
    if nxt is None:
        p["status"] = "done" if not why else "blocked"
        save(cfg, p)
        return f"{p['id']} {stage} 人工结束,流水线 {p['status']}" + (f"({why})" if why else "")
    p["stage"] = nxt
    save(cfg, p)
    gid = _start_stage(cfg, conn, p, nxt, note=f"人工从 {stage} 跳过:{note}")
    return f"{p['id']} {stage} 人工结束,进入 {nxt}(目标 {gid})"


def on_goal_done(cfg, conn, goal) -> str | None:
    """目标完成后的钩子。worker 在 _run 的最后调用。

    返回一句话(给 audit)或 None(这条目标不属于流水线)。
    """
    if not getattr(cfg, "sdlc", None) or not cfg.sdlc.enabled:
        return None
    marker = parse_marker(goal["text"])
    if marker is None:
        return None
    pid, stage, _ = marker
    p = load(cfg, pid)
    if p is None:
        return f"流水线 {pid} 不在了,目标 {goal['id']} 的产出没人接"
    if p.get("status") != "active":
        return f"流水线 {pid} 已经是 {p.get('status')} 状态,忽略"
    rec = p["stages"].get(stage) or {}
    if rec.get("goal_id") and rec["goal_id"] != goal["id"]:
        return f"流水线 {pid} 的 {stage} 已经换过目标({rec['goal_id']}),这条是旧的"

    missing = missing_artifacts(cfg, p, stage)
    if missing:
        rec["status"] = "blocked"
        rec["goal_id"] = None
        _append_history(p, "stage_incomplete", stage=stage, problems=missing)
        attempts = int(rec.get("attempts") or 1)
        if attempts < 2:
            save(cfg, p)
            note = "上一轮没落盘:\n" + "\n".join(f"- {x}" for x in missing)
            _start_stage(cfg, conn, p, stage, note=note)
            return f"{pid} {stage} 产物不全({'; '.join(missing)}),已重派一次"
        p["status"] = "blocked"
        rec["status"] = "failed"
        save(cfg, p)
        db.audit(conn, goal["id"], "sdlc_blocked",
                 f"{pid} {stage} 两次都没落盘:{'; '.join(missing)}")
        return f"{pid} {stage} 两次都没落盘,流水线停下等人看"

    rec["status"] = "done"
    rec["finished_at"] = _now()
    nxt, why = _next_stage(cfg, p, stage)
    if nxt is None:
        p["stage"] = stage
        # why 非空 = 这个阶段没通过(测试 FAIL 到上限 / 报告没有结论)。
        # 只有 why 为空才是真的走完了。
        if why:
            p["status"] = "blocked"
            save(cfg, p)
            db.audit(conn, goal["id"], "sdlc_blocked", f"{pid} {why}")
            return f"{pid} 在 {stage} 停住了:{why}"
        p["status"] = "done"
        if p.get("track") == "bugfix":
            for bid in p.get("bug_ids") or []:
                if db.get_bug(conn, bid):
                    db.set_bug(conn, bid, status=db.BUG_FIXED, pipeline_id=p["id"])
        _append_history(p, "pipeline_done", stage=stage)
        save(cfg, p)
        db.audit(conn, goal["id"], "sdlc_done", f"{pid} 走完了 {stage}")
        return f"{pid} 全流程完成"

    p["stage"] = nxt
    _append_history(p, "stage_done", stage=stage, next=nxt)
    save(cfg, p)
    _start_stage(cfg, conn, p, nxt)
    return f"{pid} {stage} 完成,进入 {nxt}"


def _next_stage(cfg, p: dict, stage: str) -> tuple[str | None, str]:
    if stage == "requirements":
        return "prototype", ""
    if stage == "prototype":
        return "dev", ""
    if stage == "dev":
        return "test", ""
    if stage == "fix":
        p["rounds"]["test"] = int(p["rounds"].get("test") or 0) + 1
        return "test", ""
    if stage == "test":
        report_path = project_dir(cfg, p) / "test/report.md"
        try:
            report = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            report = ""
        result = verdict(report)
        if result == "PASS":
            return "archive", ""
        rounds = int(p["rounds"].get("fix") or 0)
        limit = int(getattr(cfg.sdlc, "max_fix_rounds", 3) or 3)
        if result == "UNKNOWN":
            return None, "测试报告没有 VERDICT 结论,没法判断过没过"
        if rounds >= limit:
            return None, f"修了 {rounds} 轮还是 FAIL,到上限 {limit} 了"
        p["rounds"]["fix"] = rounds + 1
        return "fix", ""
    if stage == "archive":
        return None, ""
    return None, f"不认识的阶段 {stage}"


# ------------------------------------------------------------------ 本体同步

def render_ontology_section(cfg, p: dict) -> str:
    """流水线文档里那段自动生成的数据模型。改本体后整段重写。"""
    doc = ont.load(cfg)
    entities = doc.get("entities") or []
    lines = [_BEGIN, "## 数据模型(本体库自动生成,别手改这一节)", ""]
    if not entities:
        lines += ["本体库还是空的。`dots ontology sync` 采集,或 `dots ontology edit` 手写。", _END]
        return "\n".join(lines)
    lines += [
        f"本体指纹 `{ont.digest(doc)}`,更新于 {doc.get('updated_at') or '-'}。",
        "",
        "| 实体 | 名称 | 关键字段 | 关系 |",
        "| --- | --- | --- | --- |",
    ]
    for ent in entities[:60]:
        fields = ", ".join(
            f"{f.get('name')}({f.get('label') or ''})" for f in (ent.get("fields") or [])[:8]
        )
        rels = ", ".join(
            f"{r.get('field')}→{r.get('target')}" for r in (ent.get("relations") or [])[:5]
        )
        lines.append(
            f"| `{ent.get('id')}` | {ent.get('label')} | {fields} | {rels or '-'} |"
        )
    lines += ["", "字段名和枚举值以本体库为准;变更请改 ontology.json 后跑 `dots ontology apply`。", _END]
    return "\n".join(lines)


def sync_docs(cfg, doc: dict | None = None) -> dict:
    """把每条流水线每份 md 里的本体段落重写一遍。返回改了哪些文件。"""
    changed: list[str] = []
    pipelines = list_pipelines(cfg)
    for p in pipelines:
        section = render_ontology_section(cfg, p)
        root = project_dir(cfg, p)
        if not root.exists():
            continue
        for md in sorted(root.rglob("*.md")):
            if md.name.startswith("."):
                continue
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _BEGIN in text and _END in text:
                head, rest = text.split(_BEGIN, 1)
                _, tail = rest.split(_END, 1)
                new = head + section + tail
            else:
                new = text.rstrip("\n") + "\n\n" + section + "\n"
            if new != text:
                _atomic_write(md, new)
                changed.append(md.relative_to(cfg.workspace).as_posix())
        p["ontology_digest"] = ont.digest(doc or ont.load(cfg))
        save(cfg, p)
    return {"changed": changed, "pipelines": len(pipelines)}


# ------------------------------------------------------------------ 展示

def summary_line(p: dict) -> str:
    stages = TRACKS.get(p.get("track"), [])
    current = p.get("stage")
    idx = stages.index(current) + 1 if current in stages else 0
    return (
        f"{p.get('id')}  {str(p.get('status')):<8} {str(p.get('track')):<8} "
        f"{current}({idx}/{len(stages)})  {str(p.get('title'))[:40]}"
    )


def demo() -> None:
    """纯逻辑自检:阶段推进、VERDICT 解析、产物门槛。不碰模型。"""
    assert slugify("把订单导出成 CSV") == "把订单导出成-csv"
    assert slugify("a/b c") == "a-b-c"
    assert parse_marker("【SDLC 1a2b3c dev 3/5】\n正文") == ("1a2b3c", "dev", 3)
    assert parse_marker("没有标记") is None

    assert verdict("...\nVERDICT: PASS\n") == "PASS"
    assert verdict("全部通过,但 VERDICT: FAIL\n") == "FAIL"
    assert verdict("PASS 了") == "UNKNOWN"

    cfg = _DemoCfg(_demo_root())
    p = {
        "id": "abc123", "slug": "demo", "title": "示例", "requirement": "做一个东西",
        "track": "feature", "status": "active", "stage": "test",
        "rounds": {"fix": 0, "test": 0},
        "stages": {s: {"status": "pending"} for s in TRACKS["feature"]},
        "history": [],
    }
    (directory(cfg) / p["slug"] / "test").mkdir(parents=True, exist_ok=True)
    report = directory(cfg) / p["slug"] / "test/report.md"
    report.write_text("# 报告\n\nVERDICT: FAIL\n", encoding="utf-8")
    assert _next_stage(cfg, p, "test") == ("fix", "")
    assert p["rounds"]["fix"] == 1
    report.write_text("# 报告\n\nVERDICT: PASS\n", encoding="utf-8")
    assert _next_stage(cfg, p, "test") == ("archive", "")

    assert _next_stage(cfg, p, "archive") == (None, "")
    p["rounds"]["fix"] = 99
    report.write_text("# 报告\n\nVERDICT: FAIL\n", encoding="utf-8")
    nxt, why = _next_stage(cfg, p, "test")
    assert nxt is None and "上限" in why, (nxt, why)

    assert missing_artifacts(cfg, p, "test") == []
    shutil.rmtree(cfg.workspace, ignore_errors=True)
    print("sdlc: ok")


def _demo_root() -> Path:
    """自检临时目录:建在 cwd 下的普通目录,不碰 tempfile 的 0700 ACL。"""
    root = Path.cwd() / f".dots-sdlc-demo-{secrets.token_hex(4)}"
    root.mkdir(parents=True, exist_ok=True)
    return root


class _DemoCfg:
    """自检用的假配置。临时目录建在当前工作目录下 —— 有的沙箱环境里
    tempfile 建出来的目录 ACL 很严,子目录都进不去,跟被测代码无关。"""

    def __init__(self, root: Path):
        self.workspace = root
        self.sdlc = type("S", (), {
            "enabled": True, "dir": "projects", "max_fix_rounds": 3,
            "auto_from_bug": True, "stages": [],
        })()
        self.ontology = type("O", (), {
            "enabled": False, "dir": "ontology", "derived": "generated",
            "datasources": {}, "inject_prompt": True, "prompt_entities": 20,
        })()
        self.mcp = type("M", (), {"enabled": False, "servers": {}})()


if __name__ == "__main__":
    demo()
