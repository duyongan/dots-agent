"""dot.toml -> Config。全项目唯一读配置的地方。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dots.prompts import BOT_PROMPTS

DEFAULT_FILENAME = "dot.toml"

# 全局配置,`dots init --global` 写这里。
# workspace / db 都相对配置所在目录,所以全局那份的数据落在 ~/.dots/ 底下 ——
# 一份数据,cd 到哪都跟着走,不用每个目录再 init 一遍。
GLOBAL_DIR = Path.home() / ".dots"
GLOBAL_PATH = GLOBAL_DIR / DEFAULT_FILENAME


def resolve(path: str | Path | None = None) -> Path | None:
    """按 `显式路径 > ./dot.toml > ~/.dots/dot.toml` 找配置,都没有返回 None。

    显式给了路径就不兜底:`-c tyop.toml` 打错字却默默用了全局配置,
    比直接报错难查得多。
    """
    if path is not None:
        p = Path(path)
        return p if p.exists() else None
    for candidate in (Path(DEFAULT_FILENAME), GLOBAL_PATH):
        if candidate.exists():
            return candidate
    return None

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


def load(path: str | Path | None = None) -> Config:
    """找不到配置文件时给一句能看懂的报错,而不是 FileNotFoundError 堆栈。"""
    if path is not None and not Path(path).exists():
        raise SystemExit(f"找不到配置文件 {path}。")

    p = resolve(path)
    if p is None:
        raise SystemExit(
            "没找到配置。两种做法:\n"
            "  dots init --global    配一次,以后在任何目录敲 dots 都能用\n"
            "  dots init             只在这个目录用,数据和 workspace 都放这儿\n"
            "也可以用 -c 指定一份配置。"
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


# --------------------------------------------------------------- 写配置

def template_path() -> Path:
    return Path(__file__).parent / "template.toml"


def replace_section_body(text: str, section: str, body: str) -> str:
    """把 [section] 下面的内容整段换掉,直到下一个 [ 开头的行。

    比按行字符串替换稳:模板里的注释行随便改,这里都不会错位。
    """
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == f"[{section}]":
            out.append(lines[i])
            out.append(body)
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n"


def create_template_text(preset=None) -> str:
    """模板原文;探测到模型时只替换 [model] 段。"""
    text = template_path().read_text(encoding="utf-8")
    if preset:
        name, base_url, key_env, _ = preset
        body = [f'name = "{name}"']
        body.append(f'base_url = "{base_url}"' if base_url else '# base_url = ""')
        body.append(f'api_key_env = "{key_env}"' if key_env else '# api_key_env = ""')
        body.append("")
        text = replace_section_body(text, "model", "\n".join(body))
    return text


def ensure_file(dest, preset=None) -> bool:
    """不存在才写。返回是否新建。"""
    p = Path(dest)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, create_template_text(preset))
    return True


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        # JSON 的字符串语法和 TOML basic string 兼容,省得自己转义引号。
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"不支持写进配置的类型:{type(v).__name__}")


_KEY_LINE = re.compile(r"^\s*[A-Za-z0-9_.-]+\s*=")


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break
    return start, end


def apply_updates(text: str, updates: dict[str, dict]) -> str:
    """按段改 key 的赋值行,其余注释和段原样保留。

    只处理已知的标量/数组 key;不碰 [bots.*] 里的多行字符串。
    """
    lines = text.splitlines(keepends=True)

    for section, kv in updates.items():
        for key, value in kv.items():
            if value is None:
                continue
            enc = _toml_value(value)
            bounds = _section_bounds(lines, section)
            if bounds is None:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(f"\n[{section}]\n")
                lines.append(f"{key} = {enc}\n")
                continue

            start, end = bounds
            found = None
            last_key = None
            for i in range(start + 1, end):
                if _KEY_LINE.match(lines[i]):
                    last_key = i
                m = re.match(r"^(\s*)" + re.escape(key) + r"\s*=", lines[i])
                if m:
                    found = i
                    break

            if found is not None:
                line = lines[found]
                nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                indent = re.match(r"^(\s*)", line).group(1)
                lines[found] = f"{indent}{key} = {enc}{nl}"
            else:
                pos = (last_key if last_key is not None else start) + 1
                if pos > 0 and not lines[pos - 1].endswith("\n"):
                    lines[pos - 1] += "\n"
                lines.insert(pos, f"{key} = {enc}\n")

    return "".join(lines)


def save_values(path, updates: dict[str, dict]) -> Path:
    """改已有配置(不存在就从模板建)。写前用 tomllib 验一遍。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else create_template_text()
    new = apply_updates(text, updates)
    tomllib.loads(new)                 # 语法不对就别落盘
    _atomic_write(p, new)
    return p


# --------------------------------------------------------------- 密钥文件

_SECRET_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_secrets(path) -> dict[str, str]:
    """secrets.env 是 KEY=VALUE;值统一按 JSON 字符串写,也兼容裸值。"""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not _SECRET_KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        out[key] = value
    return out


def write_secrets(path, values: dict[str, str]) -> dict[str, str]:
    """合并写入;传空字符串等于删除。返回写完后的全量。"""
    p = Path(path)
    merged = read_secrets(p)
    for key, value in values.items():
        if not _SECRET_KEY.match(key):
            raise ValueError(f"环境变量名不合法:{key!r}")
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} 的值里有换行")
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    lines = [
        "# dots 本机密钥。由界面写入,也可以手改;启动时加载,已有的环境变量优先。",
        "",
    ]
    for key in sorted(merged):
        lines.append(f"{key}={json.dumps(merged[key], ensure_ascii=False)}")
    _atomic_write(p, "\n".join(lines) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return merged


def load_secrets(path) -> dict[str, str]:
    """读进进程环境。已经存在的环境变量不覆盖。"""
    values = read_secrets(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values
