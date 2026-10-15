"""技能的读写与校验。

技能 = `<workspace>/<dir>/<名>/SKILL.md`,deepagents 原生格式:
YAML frontmatter(name / description)+ markdown 正文。
deepagents 的 SkillsMiddleware 会把「名字 + 描述」塞进系统提示,
正文按需用 read_file 读 —— 渐进披露,所以描述必须短。

这里只有纯逻辑和文件 IO,不碰模型也不碰数据库,所以能单测。
文风规范提示词里写了一份,这里再校验一份:提示词会被忽略,校验不会。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

MAX_NAME = 64
# 描述每一轮都会进系统提示并被截断,超出的部分永远不起作用
MAX_DESCRIPTION = 60

# 营销词,hermes 的 house style 里点名禁掉的
BANNED = ("powerful", "comprehensive", "seamless", "advanced", "robust",
          "cutting-edge", "state-of-the-art", "effortless")


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str = ""


def validate(name: str, description: str) -> str | None:
    """返回一句给模型看的拒绝理由。通过返回 None。"""
    name = (name or "").strip()
    desc = (description or "").strip()

    if not name:
        return "name 不能为空。"
    if len(name) > MAX_NAME:
        return f"name 有 {len(name)} 字符,超过 {MAX_NAME}。"
    if not NAME_RE.match(name):
        return (
            f"name {name!r} 不合规:只能小写字母、数字和单个连字符,"
            f"不能有空格、下划线或连续连字符。"
        )

    if not desc:
        return "description 不能为空。"
    if len(desc) > MAX_DESCRIPTION:
        return (
            f"description 有 {len(desc)} 字符,上限 {MAX_DESCRIPTION}。"
            f"它每一轮都会被塞进系统提示并被截断,超出的部分永远不会起作用。"
            f"砍到 {MAX_DESCRIPTION} 以内再存。\n你写的是:{desc}"
        )
    if not desc.endswith((".", "。", "!", "?")):
        return "description 要是一句话,以句号结尾。"
    if name.replace("-", " ") in desc.lower():
        return "description 里重复了 name,不用再说一遍自己叫什么。"
    hit = [w for w in BANNED if w in desc.lower()]
    if hit:
        return f"description 里有营销词 {hit}。说清能干什么,别形容它多好。"
    return None


def render(name: str, description: str, body: str) -> str:
    """拼成 SKILL.md。描述用 JSON 转义 —— 里面可能有冒号,裸写会破坏 YAML。"""
    desc = json.dumps(description.strip(), ensure_ascii=False)
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n"


def parse(text: str) -> tuple[str, str] | None:
    """从 SKILL.md 里取出 (name, description)。坏了返回 None。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    return (name, desc) if name else None


def directory(cfg) -> Path:
    return cfg.workspace / cfg.skills.dir


def ensure_dir(cfg) -> Path:
    """提前把目录建出来。

    不建的话 deepagents 每次开跑都会警告一次
    「Cannot load skills from '/skills/': path_not_found」,纯噪音。
    """
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    return root


def skill_path(cfg, name: str) -> Path:
    return directory(cfg) / name / "SKILL.md"


def save(cfg, name: str, description: str, body: str) -> Path:
    path = skill_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(name, description, body), encoding="utf-8")
    return path


def scan(cfg) -> list[Skill]:
    """列出磁盘上的技能。按名字排序,盘上没有就是空列表。"""
    root = directory(cfg)
    if not root.exists():
        return []
    out: list[Skill] = []
    for child in sorted(root.iterdir()):
        md = child / "SKILL.md"
        if not child.is_dir() or not md.is_file():
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        parsed = parse(text)
        if parsed is None:
            out.append(Skill(child.name, "(frontmatter 坏了,加载不了)", md))
            continue
        name, desc = parsed
        body = FRONTMATTER_RE.sub("", text, count=1)
        out.append(Skill(name, desc, md, body))
    return out


def remove(cfg, name: str) -> bool:
    target = directory(cfg) / name
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def demo() -> None:
    """python -m dots.skills 直接跑。"""
    assert validate("summarize-inbox", "Turn a dropped file into an action list.") is None
    assert validate("Summarize Inbox", "x.") is not None          # 有空格和大写
    assert validate("a--b", "x.") is not None                     # 连续连字符
    assert validate("ok", "no period") is not None                # 没句号
    assert validate("ok", "A comprehensive skill that does it.") is not None   # 营销词
    assert validate("inbox", "The inbox handler.") is not None    # 重复了 name
    long_desc = "x" * 61 + "."
    assert validate("ok", long_desc) is not None
    assert validate("ok", "x" * 59 + ".") is None

    text = render("a-b", "Does a thing: with a colon.", "# Title\n\nBody")
    parsed = parse(text)
    assert parsed == ("a-b", "Does a thing: with a colon."), parsed
    assert parse("no frontmatter") is None

    # 描述里的冒号和引号不能破坏 YAML —— 这是最容易踩的一处
    tricky = 'Handles "quoted": and: colons; safely.'
    assert parse(render("t", tricky, "b")) == ("t", tricky)
    print("skills: ok")


if __name__ == "__main__":
    demo()
