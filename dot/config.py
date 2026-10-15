"""dot.toml -> Config。全项目唯一读配置的地方。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dot.prompts import BOT_PROMPTS

DEFAULT_FILENAME = "dot.toml"

# 环境变量 -> 开箱可用的模型配置。按顺序探测,第一个命中就用它。
# 三件套(name / base_url / api_key_env)都在这里定好,init 时直接写进 dot.toml。
MODEL_PRESETS: list[tuple[str, str, str | None, str | None]] = [
    # (环境变量,            name,                     base_url,                                              api_key_env)
    ("DEEPSEEK_API_KEY",  "openai:deepseek-chat",     "https://api.deepseek.com",                            None),
    ("MOONSHOT_API_KEY",  "openai:moonshot-v1-8k",    "https://api.moonshot.cn/v1",                          None),
    ("DASHSCOPE_API_KEY", "openai:qwen-plus",         "https://dashscope.aliyuncs.com/compatible-mode/v1",   None),
    ("ZHIPUAI_API_KEY",   "openai:glm-4-plus",        "https://open.bigmodel.cn/api/paas/v4",                None),
    ("ARK_API_KEY",       "openai:doubao-pro-32k",    "https://ark.cn-beijing.volces.com/api/v3",            None),
    ("SILICONFLOW_API_KEY", "openai:deepseek-ai/DeepSeek-V3", "https://api.siliconflow.cn/v1",              None),
    ("ANTHROPIC_API_KEY", "anthropic:claude-sonnet-4-6", None,                                              None),
    ("OPENAI_API_KEY",    "openai:gpt-4o",            None,                                                  None),
    ("GEMINI_API_KEY",    "google_genai:gemini-2.0-flash", None,                                             None),
]


def detect_model() -> tuple[str, str, str | None, str | None] | None:
    """扫环境变量,猜用户手上有什么 key。猜不到返回 None。

    猜到的 api_key_env 始终写回自己的变量名 —— openai: 前缀默认只认
    OPENAI_API_KEY,不写这个,DeepSeek 那类端点会 401。
    """
    for env_name, name, base_url, key_env in MODEL_PRESETS:
        if os.environ.get(env_name):
            if base_url:
                return name, base_url, key_env or env_name, None
            return name, None, key_env, None
    return None


def _env(name: str) -> str:
    return os.environ.get(name, "")


@dataclass
class ModelCfg:
    name: str = "anthropic:claude-sonnet-4-6"
    base_url: str | None = None
    # 用 openai: 前缀接第三方端点(DeepSeek 之类)时,langchain 默认只认
    # OPENAI_API_KEY。这里指个环境变量名,就不用把 key 改名。
    api_key_env: str = ""
    max_tokens: int = 4096
    temperature: float = 0.2


@dataclass
class SMTPCfg:
    host: str = ""
    port: int = 587
    user: str = ""
    sender: str = ""
    to: str = ""
    password_env: str = "DOT_SMTP_PASSWORD"
    use_tls: bool = True

    @property
    def password(self) -> str:
        return _env(self.password_env)

    @property
    def usable(self) -> bool:
        """没配 SMTP 也能跑,只是审批退化成写 audit + 打终端。"""
        return bool(self.host and self.sender and self.to)


@dataclass
class FSSubject:
    enabled: bool = True
    paths: list[str] = field(default_factory=lambda: ["inbox"])
    interval: int = 120


@dataclass
class IMAPSubject:
    enabled: bool = False
    host: str = ""
    port: int = 993
    user: str = ""
    folder: str = "INBOX"
    password_env: str = "DOT_IMAP_PASSWORD"
    interval: int = 300
    # 自建 Dovecot 用自签证书很常见。关掉校验 = 不验证书链,
    # 仍然加密。只在你自己的服务器上关。
    verify_tls: bool = True

    @property
    def password(self) -> str:
        return _env(self.password_env)


@dataclass
class ICalSubject:
    enabled: bool = False
    url: str = ""
    interval: int = 900


@dataclass
class Sources:
    fs: FSSubject = field(default_factory=FSSubject)
    imap: IMAPSubject = field(default_factory=IMAPSubject)
    ical: ICalSubject = field(default_factory=ICalSubject)


@dataclass
class BotCfg:
    """一个 bot = 一套系统提示 + 一个工具子集。

    交接是同一条对话线换个人接手,不是另起一个线程 ——
    目标 id 就是线程 id,交接后新 bot 能看到前面所有消息。
    """
    name: str
    prompt: str = ""
    tools: list[str] = field(default_factory=list)   # 空 = 全部工具


@dataclass
class SkillsCfg:
    enabled: bool = True
    # workspace 下的相对目录。技能就是 <dir>/<名>/SKILL.md
    dir: str = "skills"


@dataclass
class HandoffCfg:
    # 一个目标最多被交接几次。防乒乓 —— 两个 bot 互相甩锅能烧一整天。
    max_hops: int = 3


@dataclass
class Config:
    root: Path
    workspace: Path
    db: Path
    tick_seconds: int
    model: ModelCfg
    smtp: SMTPCfg
    sources: Sources
    rules: dict[str, str]
    approval_subject_prefix: str
    approval_timeout_hours: int
    daily_token_budget: int
    bots: dict[str, BotCfg] = field(default_factory=dict)
    skills: SkillsCfg = field(default_factory=SkillsCfg)
    handoff: HandoffCfg = field(default_factory=HandoffCfg)

    def bot(self, name: str) -> BotCfg:
        """没配过的 bot 名字也给一个默认,别因为笔误就崩。"""
        return self.bots.get(name) or BotCfg(name=name)


def _sub(raw: dict, name: str) -> dict:
    v = raw.get(name)
    return v if isinstance(v, dict) else {}


def _path(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def load(path: str | Path = DEFAULT_FILENAME) -> Config:
    """找不到配置文件时给一句能看懂的报错,而不是 FileNotFoundError 堆栈。"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"找不到配置文件 {p}。\n"
            f"在项目根目录跑 `python -m dot init` 生成一份,或加 --config 指定路径。"
        )
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{p} 不是合法的 TOML: {e}")

    root = p.resolve().parent
    dot = _sub(raw, "dot")

    m = _sub(raw, "model")
    model = ModelCfg(
        name=m.get("name", ModelCfg.name),
        base_url=m.get("base_url") or None,
        api_key_env=m.get("api_key_env", ""),
        max_tokens=int(m.get("max_tokens", 4096)),
        temperature=float(m.get("temperature", 0.2)),
    )

    s = _sub(raw, "smtp")
    smtp = SMTPCfg(
        host=s.get("host", ""),
        port=int(s.get("port", 587)),
        user=s.get("user", ""),
        sender=s.get("sender", ""),
        to=s.get("to", ""),
        password_env=s.get("password_env", "DOT_SMTP_PASSWORD"),
        use_tls=bool(s.get("use_tls", True)),
    )

    src = _sub(raw, "sources")
    f, i, c = _sub(src, "fs"), _sub(src, "imap"), _sub(src, "ical")
    sources = Sources(
        fs=FSSubject(
            enabled=bool(f.get("enabled", True)),
            paths=list(f.get("paths", ["inbox"])),
            interval=int(f.get("interval", 120)),
        ),
        imap=IMAPSubject(
            enabled=bool(i.get("enabled", False)),
            host=i.get("host", ""),
            port=int(i.get("port", 993)),
            user=i.get("user", ""),
            folder=i.get("folder", "INBOX"),
            password_env=i.get("password_env", "DOT_IMAP_PASSWORD"),
            interval=int(i.get("interval", 300)),
            verify_tls=bool(i.get("verify_tls", True)),
        ),
        ical=ICalSubject(
            enabled=bool(c.get("enabled", False)),
            url=c.get("url", ""),
            interval=int(c.get("interval", 900)),
        ),
    )

    ap = _sub(raw, "approval")
    safety = _sub(raw, "safety")

    # 默认永远有一个 dot bot,配置里写不写都在
    bots: dict[str, BotCfg] = {"dot": BotCfg(name="dot", prompt=BOT_PROMPTS["dot"])}
    for name, spec in _sub(raw, "bots").items():
        spec = spec if isinstance(spec, dict) else {}
        bots[name] = BotCfg(
            name=name,
            prompt=spec.get("prompt") or BOT_PROMPTS.get(name, ""),
            tools=list(spec.get("tools", [])),
        )

    sk = _sub(raw, "skills")
    skills = SkillsCfg(
        enabled=bool(sk.get("enabled", True)),
        dir=str(sk.get("dir", "skills")).strip("/") or "skills",
    )

    ho = _sub(raw, "handoff")
    handoff = HandoffCfg(max_hops=int(ho.get("max_hops", 3)))

    return Config(
        root=root,
        workspace=_path(root, dot.get("workspace", "workspace")),
        db=_path(root, dot.get("db", "dot.db")),
        tick_seconds=int(dot.get("tick_seconds", 60)),
        model=model,
        smtp=smtp,
        sources=sources,
        rules={str(k): str(v) for k, v in _sub(ap, "rules").items()},
        approval_subject_prefix=ap.get("subject_prefix", "[dot#"),
        approval_timeout_hours=int(ap.get("timeout_hours", 72)),
        daily_token_budget=int(safety.get("daily_token_budget", 0)),
        bots=bots,
        skills=skills,
        handoff=handoff,
    )
