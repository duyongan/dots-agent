"""本地图形界面:一个只听 127.0.0.1 的 JSON API + React 静态页。

和 web.py 的只读活动页不是一回事:这里能聊天、改配置、管技能、批动作,
所以写请求要带启动时生成的 token。页面由同源服务,恶意站点读不到它。

tick 和聊天各用各的线程、各用各的 RunCtx(线程局部)和 checkpointer 连接,
所以可以并发。建图必须在真正 invoke 它的那条线程里做 —— 工具闭包捏的
就是建图时的 ctx。
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from dots import __version__, approvals, db, graph, worker
from dots import config as config_mod
from dots import rules as rules_mod
from dots import skills as skills_mod

WEBUI = Path(__file__).parent / "webui"

PRESET_LABELS = {
    "DEEPSEEK_API_KEY": "DeepSeek",
    "MOONSHOT_API_KEY": "Kimi",
    "DASHSCOPE_API_KEY": "通义千问",
    "ZHIPUAI_API_KEY": "智谱 GLM",
    "ARK_API_KEY": "豆包",
    "SILICONFLOW_API_KEY": "硅基流动",
    "ANTHROPIC_API_KEY": "Claude",
    "OPENAI_API_KEY": "OpenAI",
    "GEMINI_API_KEY": "Gemini",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or {}


# --------------------------------------------------------------------- 路径

def runtime_root() -> Path:
    """放配置和数据的地方。

    frozen 就跟着 exe 走(安装目录,每用户安装可写);源码模式用 ~/.dots。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return config_mod.GLOBAL_DIR


def gui_config_path(explicit=None) -> Path:
    if explicit:
        return Path(explicit)
    if getattr(sys, "frozen", False):
        # 安装包按用户要求把配置和数据放在安装目录,不去翻 ~/.dots。
        return runtime_root() / config_mod.DEFAULT_FILENAME
    found = config_mod.resolve(None)
    if found:
        return found
    return runtime_root() / config_mod.DEFAULT_FILENAME


def secrets_path(config_path) -> Path:
    return Path(config_path).parent / "secrets.env"


def _jsonable(row) -> dict:
    return {k: row[k] for k in row.keys()}


# --------------------------------------------------------------------- 服务

class ChatSession:
    def __init__(self, cfg):
        self.conn = db.connect(cfg.db)
        db.init(self.conn)
        try:
            self.agents, self.saver = worker.chat_agents(cfg, self.conn)
        except BaseException:
            self.conn.close()
            raise
        # chat_agents 在本线程 use_or_create 过;工具闭包捏的就是这个 ctx。
        self.ctx = graph.current()
        if self.ctx is None:
            self.conn.close()
            self.saver.conn.close()
            raise RuntimeError("chat_agents 没建立 RunCtx")
        self.pending = self._initial_pending()

    def _initial_pending(self):
        """进程重启后,checkpoint 里可能还停在一个 interrupt 上。"""
        try:
            state = self.agents["dot"].get_state(worker.chat_config())
            interrupts = getattr(state, "interrupts", None) or ()
            if interrupts:
                return getattr(interrupts[0], "value", None) or {}
        except Exception:
            pass
        return None

    def close(self):
        try:
            self.conn.close()
        finally:
            self.saver.conn.close()


class Service:
    def __init__(self, config_path, *, dry_run: bool = False,
                 static_dir: Path | None = None):
        self.config_path = Path(config_path)
        self.dry_run = dry_run
        self.static_dir = Path(static_dir) if static_dir else WEBUI
        self.token = secrets.token_urlsafe(24)

        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.state_lock = threading.RLock()
        # 可重入:chat_history 会在 chat_send 持锁时被同一线程再进一次。
        self.chat_lock = threading.RLock()

        self.cfg = None
        self.tick_conn = None
        self.agents = None
        self.saver = None
        self.tick_ctx = None
        self.chat = None
        self.chat_dirty = False
        self.config_error = None
        self.needs_reload = False
        self._recovered = False

        self.tick_thread = None
        self.server = None
        self.port = 0

    # -- 生命周期 -----------------------------------------------------

    def bootstrap(self) -> None:
        created = config_mod.ensure_file(self.config_path, config_mod.detect_model())
        if created:
            print(f"[dots] 首次启动,已生成配置 {self.config_path}")
        config_mod.load_secrets(secrets_path(self.config_path))
        self._load_cfg()
        self.tick_thread = threading.Thread(
            target=self._tick_loop, name="dots-gui-tick", daemon=True
        )
        self.tick_thread.start()

    def _load_cfg(self) -> None:
        """只读配置,给 HTTP 端和界面看;建图归 tick 线程。"""
        try:
            cfg = config_mod.load(self.config_path)
        except SystemExit as e:
            self.config_error = str(e)
            with self.state_lock:
                self.cfg = None
            return
        with self.state_lock:
            self.cfg = cfg
        try:
            cfg.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.config_error = f"工作区建不出来:{e}"

    def shutdown(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.tick_thread is not None:
            self.tick_thread.join(timeout=8)
        if self.chat_lock.acquire(timeout=3):
            try:
                self._close_chat_locked()
            finally:
                self.chat_lock.release()

    def _rebuild_tick(self) -> None:
        """在 tick 线程里重建运行态。

        graph 的 RunCtx 是线程局部的,建图和 invoke 必须在同一条线程 ——
        所以这个方法只能被 tick 线程调用。
        """
        self._close_tick_runtime()
        self.config_error = None
        try:
            cfg = config_mod.load(self.config_path)
        except SystemExit as e:
            self.config_error = str(e)
            with self.state_lock:
                self.cfg = None
            return
        with self.state_lock:
            self.cfg = cfg
        try:
            cfg.workspace.mkdir(parents=True, exist_ok=True)
            conn = db.connect(cfg.db)
            db.init(conn)
            self.tick_conn = conn
            if not self._recovered:
                worker.recover_stale_runs(cfg, conn)
                self._recovered = True
            self.agents, self.saver = worker.build_agents(cfg, conn)
            self.tick_ctx = graph.current()
        except SystemExit as e:
            self.config_error = str(e)
        except Exception as e:
            self.config_error = f"{type(e).__name__}: {e}"

    def _close_tick_runtime(self) -> None:
        if self.saver is not None:
            try:
                self.saver.conn.close()
            except Exception:
                pass
        if self.tick_conn is not None:
            try:
                self.tick_conn.close()
            except Exception:
                pass
        self.agents = None
        self.saver = None
        self.tick_conn = None
        self.tick_ctx = None
        graph.set_current(None)

    def _close_chat_locked(self) -> None:
        if self.chat is not None:
            try:
                self.chat.close()
            except Exception:
                pass
            self.chat = None

    def reload(self, timeout: float = 1.5) -> bool:
        """叫醒 tick 线程重建;它正跑着就等一会儿。"""
        self.needs_reload = True
        self.wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.needs_reload and self.agents is not None:
                return True
            time.sleep(0.05)
        return False

    def _tick_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    if self.needs_reload or self.agents is None:
                        self._rebuild_tick()
                        self.needs_reload = False
                    if self.agents is not None:
                        stats = worker.tick(
                            self.cfg, self.tick_conn, self.agents, dry_run=self.dry_run
                        )
                        line = worker.summarize(stats)
                        if line:
                            print(f"[dots] {line}")
                except Exception:
                    text = traceback.format_exc()[-1200:]
                    print(text, file=sys.stderr)
                    if self.tick_conn is not None:
                        try:
                            db.audit(self.tick_conn, None, "tick_error", text)
                        except Exception:
                            pass
                wait = self.cfg.tick_seconds if self.cfg is not None else 5
                self.wake.wait(max(1, wait))
                self.wake.clear()
        finally:
            self._close_tick_runtime()

    # -- 聊天 ---------------------------------------------------------

    def _ensure_chat_locked(self) -> ChatSession:
        if self.chat is not None and not self.chat_dirty:
            return self.chat
        if self.chat is not None:
            self._close_chat_locked()
        self.chat_dirty = False
        try:
            cfg = config_mod.load(self.config_path)
        except SystemExit as e:
            raise ApiError(409, "model_not_ready", str(e))
        with self.state_lock:
            self.cfg = cfg
        try:
            self.chat = ChatSession(cfg)
        except SystemExit as e:
            raise ApiError(409, "model_not_ready", str(e))
        except Exception as e:
            raise ApiError(409, "model_not_ready", f"{type(e).__name__}: {e}")
        self.chat.pending = self._pending_from_state(self.chat) or self.chat.pending
        return self.chat

    def _pending_from_state(self, session):
        try:
            state = session.agents["dot"].get_state(worker.chat_config())
            interrupts = getattr(state, "interrupts", None) or ()
            if interrupts:
                return getattr(interrupts[0], "value", None) or {}
        except Exception:
            pass
        return None

    def chat_history(self) -> dict:
        if not self.chat_lock.acquire(blocking=False):
            pending = self.chat.pending if self.chat is not None else None
            return {"messages": [], "interrupt": pending, "busy": True}
        try:
            session = self.chat
            if session is None:
                return {"messages": [], "interrupt": None, "busy": False}
            state = session.agents["dot"].get_state(worker.chat_config())
            messages = worker._messages(state)
            out = []
            for m in messages:
                kind = getattr(m, "type", "")
                if kind in ("human", "user"):
                    role = "user"
                elif kind in ("ai", "assistant"):
                    role = "assistant"
                else:
                    continue
                text = worker._flatten(getattr(m, "content", "")).strip()
                if text:
                    out.append({"role": role, "content": text})
            pending = self._pending_from_state(session)
            if pending:
                session.pending = pending
            return {"messages": out, "interrupt": session.pending, "busy": False}
        finally:
            self.chat_lock.release()

    def _chat_result(self, session, result, interrupt, used: int) -> dict:
        history = self.chat_history()
        reply = None if interrupt else worker._last_reply(worker._messages(result))
        return {
            "messages": history["messages"],
            "reply": reply,
            "interrupt": interrupt,
            "tokens": used,
        }

    def chat_send(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ApiError(400, "validation", "消息不能为空。")
        if not self.chat_lock.acquire(blocking=False):
            raise ApiError(409, "busy", "上一轮还在跑,等它结束。")
        try:
            session = self._ensure_chat_locked()
            if session.pending:
                raise ApiError(409, "pending", "有个动作还等你批准,先处理它。")
            agent = session.agents["dot"]
            before = worker._messages(agent.get_state(worker.chat_config()))
            result, interrupt = worker.chat_turn(
                agent, session.ctx, {"messages": [HumanMessage(text)]}
            )
            after = worker._messages(result)
            used = worker._tokens_new(before, after)
            db.audit(session.conn, None, "chat", text[:200], tokens=used)
            session.pending = interrupt
            return self._chat_result(session, result, interrupt, used)
        finally:
            self.chat_lock.release()

    def chat_resume(self, approved: bool) -> dict:
        if not self.chat_lock.acquire(blocking=False):
            raise ApiError(409, "busy", "上一轮还在跑。")
        try:
            session = self.chat
            if session is None:
                raise ApiError(409, "conflict", "没有挂起的对话动作。")
            pending = session.pending or self._pending_from_state(session)
            if not pending:
                raise ApiError(409, "conflict", "没有挂起的对话动作。")
            agent = session.agents["dot"]
            before = worker._messages(agent.get_state(worker.chat_config()))
            result, interrupt = worker.chat_turn(
                agent, session.ctx,
                Command(resume={"approved": bool(approved)}),
            )
            after = worker._messages(result)
            used = worker._tokens_new(before, after)
            db.audit(session.conn, None, "chat_decision",
                     f"{pending.get('action')} -> {'approved' if approved else 'rejected'}",
                     tokens=used)
            session.pending = interrupt
            return self._chat_result(session, result, interrupt, used)
        finally:
            self.chat_lock.release()

    # -- 配置 ---------------------------------------------------------

    def _secret_names(self, cfg=None) -> set[str]:
        names = set(PRESET_LABELS)
        cfg = cfg if cfg is not None else self.cfg
        if cfg is not None:
            for name in (cfg.model.api_key_env,
                         cfg.smtp.password_env,
                         cfg.sources.imap.password_env):
                if name:
                    names.add(name)
        return names

    def secrets_dto(self, cfg=None) -> dict:
        return {
            "path": str(secrets_path(self.config_path)),
            "set": {name: bool(os.environ.get(name)) for name in sorted(self._secret_names(cfg))},
        }

    def health(self) -> dict:
        return {
            "version": __version__,
            "config_path": str(self.config_path),
            "config_error": self.config_error,
            "model_ready": bool(self.agents),
            "chat_pending": bool(self.chat and self.chat.pending),
            "dry_run": self.dry_run,
            "workspace": str(self.cfg.workspace) if self.cfg else None,
            "db": str(self.cfg.db) if self.cfg else None,
        }

    def config_dto(self, cfg=None) -> dict:
        cfg = cfg if cfg is not None else self.cfg
        if cfg is None:
            raise ApiError(500, "config", self.config_error or "配置读不出来。")
        return {
            "path": str(self.config_path),
            "secrets_path": str(secrets_path(self.config_path)),
            "dot": {
                "tick_seconds": cfg.tick_seconds,
                "workspace": str(cfg.workspace),
                "db": str(cfg.db),
            },
            "model": {
                "name": cfg.model.name,
                "base_url": cfg.model.base_url or "",
                "api_key_env": cfg.model.api_key_env,
                "max_tokens": cfg.model.max_tokens,
                "temperature": cfg.model.temperature,
            },
            "smtp": {
                "host": cfg.smtp.host, "port": cfg.smtp.port, "user": cfg.smtp.user,
                "sender": cfg.smtp.sender, "to": cfg.smtp.to,
                "password_env": cfg.smtp.password_env, "use_tls": cfg.smtp.use_tls,
            },
            "sources": {
                "fs": {
                    "enabled": cfg.sources.fs.enabled,
                    "paths": cfg.sources.fs.paths,
                    "interval": cfg.sources.fs.interval,
                },
                "imap": {
                    "enabled": cfg.sources.imap.enabled, "host": cfg.sources.imap.host,
                    "port": cfg.sources.imap.port, "user": cfg.sources.imap.user,
                    "folder": cfg.sources.imap.folder,
                    "password_env": cfg.sources.imap.password_env,
                    "interval": cfg.sources.imap.interval,
                    "verify_tls": cfg.sources.imap.verify_tls,
                },
                "ical": {
                    "enabled": cfg.sources.ical.enabled, "url": cfg.sources.ical.url,
                    "interval": cfg.sources.ical.interval,
                },
            },
            "approval": {
                "subject_prefix": cfg.approval_subject_prefix,
                "timeout_hours": cfg.approval_timeout_hours,
                "rules": dict(cfg.rules),
            },
            "skills": {"enabled": cfg.skills.enabled, "dir": cfg.skills.dir},
            "handoff": {"max_hops": cfg.handoff.max_hops},
            "safety": {"daily_token_budget": cfg.daily_token_budget},
            "bots": sorted(cfg.bots),
            "secrets": self.secrets_dto(cfg),
        }

    @staticmethod
    def model_presets() -> list[dict]:
        out = []
        for env_name, name, base_url, key_env in config_mod.MODEL_PRESETS:
            out.append({
                "env": env_name,
                "label": PRESET_LABELS.get(env_name, env_name),
                "name": name,
                "base_url": base_url or "",
                "api_key_env": key_env or env_name,
            })
        return out

    def save_config(self, body: dict) -> dict:
        updates = _config_updates(body)
        try:
            config_mod.save_values(self.config_path, updates)
        except (ValueError, TypeError, OSError) as e:
            raise ApiError(400, "validation", f"配置没写成:{e}")
        secret_values = body.get("secrets") or {}
        if secret_values:
            try:
                config_mod.write_secrets(secrets_path(self.config_path), secret_values)
            except ValueError as e:
                raise ApiError(400, "validation", str(e))
            for key, value in secret_values.items():
                if value == "":
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)
        self.chat_dirty = True
        restarted = self.reload()
        # reload 没赶上时运行态还是旧的,但文件已经是新的;
        # 回给界面的 DTO 一定要从文件重读,否则刚填的值会被旧值盖掉。
        fresh = None
        try:
            fresh = config_mod.load(self.config_path)
        except SystemExit:
            pass
        dto = self.config_dto(fresh) if fresh is not None else None
        return {
            "ok": dto is not None,
            "config": dto,
            "config_error": self.config_error if restarted else None,
            "reloaded_now": restarted,
            "secrets": self.secrets_dto(fresh),
        }

    def save_secrets(self, values: dict) -> dict:
        try:
            merged = config_mod.write_secrets(secrets_path(self.config_path), values)
        except ValueError as e:
            raise ApiError(400, "validation", str(e))
        for key, value in values.items():
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        self.chat_dirty = True
        self.reload()
        return {"ok": True, "set": {k: bool(os.environ.get(k)) for k in merged},
                "secrets": self.secrets_dto()}

    # -- 技能 ---------------------------------------------------------

    def skills_list(self) -> dict:
        cfg = self.cfg
        if cfg is None:
            raise ApiError(500, "config", self.config_error or "配置读不出来。")
        items = []
        for s in skills_mod.scan(cfg):
            items.append({
                "name": s.name,
                "description": s.description,
                "broken": s.description.startswith("(frontmatter"),
            })
        return {"enabled": cfg.skills.enabled, "dir": cfg.skills.dir, "skills": items}

    def skill_get(self, name: str) -> dict:
        _check_skill_name(name)
        cfg = self.cfg
        found = next((s for s in skills_mod.scan(cfg) if s.name == name), None)
        if found is None:
            raise ApiError(404, "not_found", "没有这个技能。")
        return {
            "name": found.name, "description": found.description,
            "body": found.body, "path": str(found.path),
        }

    def skill_save(self, name: str, description: str, body: str, *, create: bool) -> dict:
        _check_skill_name(name)
        cfg = self.cfg
        if not cfg.skills.enabled:
            raise ApiError(409, "disabled", "技能功能被关掉了。")
        problem = skills_mod.validate(name, description)
        if problem:
            raise ApiError(400, "validation", problem, {"description": problem})
        existing = {s.name for s in skills_mod.scan(cfg)}
        if create and name in existing:
            raise ApiError(409, "conflict", "同名技能已经存在。")
        if not create and name not in existing:
            raise ApiError(404, "not_found", "没有这个技能。")
        path = skills_mod.save(cfg, name, description.strip(), body)
        self._audit("save_skill_ui", f"{name}: {description[:80]}")
        return {"ok": True, "name": name, "path": str(path)}

    def skill_delete(self, name: str) -> dict:
        _check_skill_name(name)
        if self.cfg is None:
            raise ApiError(500, "config", "配置读不出来。")
        if not skills_mod.remove(self.cfg, name):
            raise ApiError(404, "not_found", "没有这个技能。")
        self._audit("remove_skill_ui", name)
        return {"ok": True}

    def _audit(self, kind: str, detail: str) -> None:
        """HTTP 线程写审计用自己的一条短连接,别借 tick 线程那条。"""
        if self.cfg is None:
            return
        conn = db.connect(self.cfg.db)
        try:
            db.audit(conn, None, kind, detail)
        finally:
            conn.close()


def _check_skill_name(name: str) -> None:
    if not skills_mod.NAME_RE.match(name or ""):
        raise ApiError(400, "validation", "技能名只能是小写字母、数字和单个连字符。")


def _int(value, field: str, minimum: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "validation", f"{field} 要是整数。", {field: "要是整数"})
    if n < minimum:
        raise ApiError(400, "validation", f"{field} 不能小于 {minimum}。",
                       {field: f"不能小于 {minimum}"})
    return n


def _bool(value) -> bool:
    return bool(value)


def _temperature(value) -> float:
    f = float(value)
    if not 0 <= f <= 2:
        raise ApiError(400, "validation", "temperature 要在 0 到 2 之间。")
    return f


def _skills_dir(value) -> str:
    s = str(value).strip().strip("/")
    p = Path(s)
    if not s or p.is_absolute() or ".." in p.parts:
        raise ApiError(400, "validation", "技能目录必须是工作区里的相对路径。")
    return s


def _config_updates(body: dict) -> dict[str, dict]:
    """把界面提交的 DTO 翻成 [段] -> {key: value}。只认白名单。"""
    updates: dict[str, dict] = {}

    def take(section: str, src, mapping: dict[str, callable]):
        if not isinstance(src, dict):
            return
        out = {}
        for key, conv in mapping.items():
            if key in src:
                try:
                    out[key] = conv(src[key])
                except (TypeError, ValueError):
                    raise ApiError(400, "validation", f"{section}.{key} 格式不对。")
        if out:
            updates[section] = out

    take("dot", body.get("dot"), {"tick_seconds": lambda v: _int(v, "tick_seconds", 1)})
    take("model", body.get("model"), {
        "name": str,
        "base_url": lambda v: str(v or ""),
        "api_key_env": lambda v: str(v or ""),
        "max_tokens": lambda v: _int(v, "max_tokens", 1),
        "temperature": _temperature,
    })
    take("smtp", body.get("smtp"), {
        "host": str, "port": lambda v: _int(v, "port", 1),
        "user": str, "sender": str, "to": str,
        "password_env": str, "use_tls": _bool,
    })
    sources = body.get("sources") or {}
    take("sources.fs", sources.get("fs"), {
        "enabled": _bool,
        "paths": lambda v: [str(x).strip() for x in v if str(x).strip()],
        "interval": lambda v: _int(v, "interval", 1),
    })
    take("sources.imap", sources.get("imap"), {
        "enabled": _bool, "host": str, "port": lambda v: _int(v, "port", 1),
        "user": str, "folder": str, "password_env": str,
        "interval": lambda v: _int(v, "interval", 1), "verify_tls": _bool,
    })
    take("sources.ical", sources.get("ical"), {
        "enabled": _bool, "url": str, "interval": lambda v: _int(v, "interval", 1),
    })

    approval = body.get("approval") or {}
    take("approval", approval, {
        "subject_prefix": str,
        "timeout_hours": lambda v: _int(v, "timeout_hours", 0),
    })
    rules = approval.get("rules")
    if isinstance(rules, dict):
        clean = {}
        for key, value in rules.items():
            if not re.match(r"^[A-Za-z0-9_*]+$", str(key)):
                raise ApiError(400, "validation", f"动作名不合法:{key!r}")
            if value not in (rules_mod.ALLOW, rules_mod.ASK, rules_mod.DENY):
                raise ApiError(400, "validation",
                               f"{key} 只能是 allow / ask / deny。")
            clean[str(key)] = str(value)
        if clean:
            updates["approval.rules"] = clean

    take("skills", body.get("skills"), {
        "enabled": _bool,
        "dir": _skills_dir,
    })
    take("handoff", body.get("handoff"), {
        "max_hops": lambda v: _int(v, "max_hops", 0),
    })
    take("safety", body.get("safety"), {
        "daily_token_budget": lambda v: _int(v, "daily_token_budget", 0),
    })
    return updates


# --------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "dots-gui"

    @property
    def service(self) -> Service:
        return self.server.service

    # -- 基础设施 -----------------------------------------------------

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in {
            f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
        }

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _authorized(self) -> bool:
        token = self.headers.get("X-Dots-Token") or ""
        return hmac.compare_digest(token, self.service.token)

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, err: ApiError) -> None:
        self._json(err.status, {
            "error": {"code": err.code, "message": err.message, "fields": err.fields}
        })

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4 * 1024 * 1024:
            raise ApiError(413, "too_large", "请求太大了。")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "validation", "请求不是合法 JSON。")
        if not isinstance(body, dict):
            raise ApiError(400, "validation", "请求体要是对象。")
        return body

    def _guard_write(self) -> None:
        if not self._host_ok():
            raise ApiError(403, "forbidden", "Host 不对。")
        if not self._origin_ok():
            raise ApiError(403, "forbidden", "Origin 不对。")
        if not self._authorized():
            raise ApiError(403, "forbidden", "token 不对,刷新页面重试。")

    # -- 路由 ---------------------------------------------------------

    def do_GET(self):  # noqa: N802
        try:
            if not self._host_ok():
                raise ApiError(403, "forbidden", "Host 不对。")
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                self._route_get(path)
            else:
                self._static(path)
        except ApiError as e:
            self._error(e)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": {"code": "internal", "message": "服务器出错了。"}})

    def do_POST(self):  # noqa: N802
        self._mutate("POST")

    def do_PUT(self):  # noqa: N802
        self._mutate("PUT")

    def do_DELETE(self):  # noqa: N802
        self._mutate("DELETE")

    def _mutate(self, method: str) -> None:
        try:
            self._guard_write()
            path = urlparse(self.path).path
            body = self._read_json()
            self._route_write(method, path, body)
        except ApiError as e:
            self._error(e)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": {"code": "internal", "message": "服务器出错了。"}})

    def _route_get(self, path: str) -> None:
        svc = self.service
        if path == "/api/health":
            self._json(200, svc.health())
            return
        if path == "/api/dashboard":
            self._json(200, self._dashboard())
            return
        if path == "/api/goals":
            self._json(200, {"goals": self._goals()})
            return
        if path == "/api/chat":
            self._json(200, svc.chat_history())
            return
        if path == "/api/config":
            self._json(200, svc.config_dto())
            return
        if path == "/api/model-presets":
            self._json(200, {"presets": svc.model_presets()})
            return
        if path == "/api/secrets":
            self._json(200, svc.secrets_dto())
            return
        if path == "/api/skills":
            self._json(200, svc.skills_list())
            return
        if path.startswith("/api/skills/"):
            self._json(200, svc.skill_get(unquote(path.rsplit("/", 1)[-1])))
            return
        raise ApiError(404, "not_found", "没有这个接口。")

    def _route_write(self, method: str, path: str, body: dict) -> None:
        svc = self.service
        if method == "POST" and path == "/api/chat":
            self._json(200, svc.chat_send(str(body.get("text", ""))))
            return
        if method == "POST" and path == "/api/chat/resume":
            self._json(200, svc.chat_resume(bool(body.get("approved"))))
            return
        if method == "POST" and path == "/api/goals":
            self._json(201, self._new_goal(body))
            return
        if method == "POST" and path == "/api/shutdown":
            self._json(202, {"ok": True})
            threading.Thread(target=self._shutdown, daemon=True).start()
            return
        if method == "POST" and path.startswith("/api/approvals/"):
            self._json(200, self._decide(unquote(path.rsplit("/", 1)[-1]), body))
            return
        if method == "PUT" and path == "/api/config":
            self._json(200, svc.save_config(body))
            return
        if method == "PUT" and path == "/api/secrets":
            self._json(200, svc.save_secrets(body.get("values") or {}))
            return
        if path == "/api/skills" and method == "POST":
            self._json(201, svc.skill_save(
                str(body.get("name", "")), str(body.get("description", "")),
                str(body.get("body", "")), create=True,
            ))
            return
        if path.startswith("/api/skills/"):
            name = unquote(path.rsplit("/", 1)[-1])
            if method == "PUT":
                self._json(200, svc.skill_save(
                    name, str(body.get("description", "")),
                    str(body.get("body", "")), create=False,
                ))
                return
            if method == "DELETE":
                self._json(200, svc.skill_delete(name))
                return
        raise ApiError(404, "not_found", "没有这个接口。")

    def _shutdown(self) -> None:
        self.service.shutdown()
        if self.service.server is not None:
            self.service.server.shutdown()

    def _conn(self):
        cfg = self.service.cfg
        if cfg is None:
            raise ApiError(503, "config", self.service.config_error or "配置读不出来。")
        conn = db.connect(cfg.db)
        db.init(conn)
        return conn

    def _goals(self) -> list[dict]:
        conn = self._conn()
        try:
            return [_jsonable(g) for g in db.list_goals(conn, limit=100)]
        finally:
            conn.close()

    def _dashboard(self) -> dict:
        svc = self.service
        if svc.cfg is None:
            raise ApiError(503, "config", svc.config_error or "配置读不出来。")
        conn = self._conn()
        try:
            goals = [_jsonable(g) for g in db.list_goals(conn, limit=60)]
            pending = []
            for a in db.pending_approvals(conn):
                item = _jsonable(a)
                try:
                    item["args"] = json.loads(item.get("args") or "{}")
                except (TypeError, ValueError):
                    pass
                pending.append(item)
            audit = [_jsonable(r) for r in db.recent_audit(conn, 80)]
            today = db.tokens_since(conn, worker._local_midnight())
            total = db.tokens_since(conn, 0)
        finally:
            conn.close()
        return {
            "goals": goals,
            "pending": pending,
            "audit": audit,
            "cost": {
                "today": today,
                "total": total,
                "budget": svc.cfg.daily_token_budget,
            },
            "config_error": svc.config_error,
        }

    def _new_goal(self, body: dict) -> dict:
        text = str(body.get("text", "")).strip()
        if not text:
            raise ApiError(400, "validation", "目标不能为空。")
        bot = str(body.get("bot") or "dot").strip().lower()
        cfg = self.service.cfg
        if bot not in cfg.bots:
            raise ApiError(400, "validation", f"没有叫 {bot} 的 bot。")
        conn = self._conn()
        try:
            gid = db.add_goal(conn, text, origin="gui", status=db.READY, bot=bot)
            db.audit(conn, gid, "goal_created", f"[{bot}] {text[:200]}")
        finally:
            conn.close()
        self.service.wake.set()
        return {"id": gid, "status": db.READY}

    def _decide(self, token: str, body: dict) -> dict:
        decision = body.get("decision")
        if decision not in (approvals.APPROVED, approvals.REJECTED):
            raise ApiError(400, "validation", "decision 只能是 approved 或 rejected。")
        conn = self._conn()
        try:
            row = db.get_approval(conn, token)
            if row is None:
                raise ApiError(404, "not_found", "没有这个审批。")
            if not db.resolve_approval(conn, token, decision):
                raise ApiError(409, "conflict", "这个审批已经处理过了。")
            db.audit(conn, row["goal_id"], "approval_gui",
                     f"{token} -> {decision}")
        finally:
            conn.close()
        self.service.wake.set()
        return {"ok": True, "token": token, "decision": decision,
                "goal_id": row["goal_id"]}

    # -- 静态文件 -----------------------------------------------------

    def _static(self, path: str) -> None:
        root = self.service.static_dir
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            raise ApiError(404, "not_found", "没有这个文件。")
        if not target.is_file():
            if rel != "index.html" and "." in Path(rel).name:
                raise ApiError(404, "not_found", "没有这个文件。")
            target = root / "index.html"
        if not target.is_file():
            body = ("前端还没构建。在仓库里跑:cd frontend && npm ci && npm run build"
                    ).encode("utf-8")
            self._send(200, body, "text/plain; charset=utf-8")
            return
        if target.name == "index.html":
            text = target.read_text(encoding="utf-8")
            text = text.replace("%DOTS_TOKEN%", self.service.token)
            body = text.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if "/assets/" in path else "no-store"
        self._send(200, target.read_bytes(), ctype, cache)

    def log_message(self, *args):
        pass


def make_server(service: Service, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.service = service
    service.server = server
    service.port = server.server_address[1]
    return server


def run_gui(config=None, port: int = 8765, open_browser: bool = True,
            dry_run: bool = False) -> int:
    path = gui_config_path(config)
    try:
        service = Service(path, dry_run=dry_run)
        service.bootstrap()
    except SystemExit as e:
        print(f"[dots] {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[dots] 数据目录不可写({path.parent}):{e}", file=sys.stderr)
        return 1

    try:
        server = make_server(service, port)
    except OSError as e:
        print(f"[dots] 界面起不来(127.0.0.1:{port}):{e}\n"
              f"换个端口:dots gui --port 8899", file=sys.stderr)
        service.shutdown()
        return 1

    url = f"http://127.0.0.1:{service.port}/"
    print(f"[dots] 图形界面 {url}")
    print(f"[dots] 配置 {service.config_path}")
    if service.config_error:
        print(f"[dots] 还差一步:{service.config_error}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[dots] 停了。")
    finally:
        service.shutdown()
        server.server_close()
    return 0
