"""改 dot.toml / secrets.env。不联网。"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

from dots import config


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dots-cfg-"))


def test_patch_preserves_comments_and_other_tables() -> None:
    p = _tmpdir() / "dot.toml"
    assert config.ensure_file(p) is True
    assert config.ensure_file(p) is False          # 已存在就不动

    config.save_values(p, {
        "model": {
            "name": "openai:deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
        "sources.fs": {"paths": ["inbox", "notes"]},
        "approval.rules": {"send_email": "allow"},
        "safety": {"daily_token_budget": 12345},
    })

    text = p.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert raw["model"]["name"] == "openai:deepseek-chat"
    assert raw["model"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert raw["sources"]["fs"]["paths"] == ["inbox", "notes"]
    assert raw["approval"]["rules"]["send_email"] == "allow"
    assert raw["safety"]["daily_token_budget"] == 12345
    assert "心跳间隔" in text                      # 注释还在
    assert "[bots.ops]" in text                    # 没碰的段还在


def test_missing_key_is_inserted_inside_its_table() -> None:
    p = _tmpdir() / "dot.toml"
    config.ensure_file(p)
    config.save_values(p, {"model": {"reasoning_effort": "high"}})

    text = p.read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert raw["model"]["reasoning_effort"] == "high"
    assert text.index("reasoning_effort") < text.index("\n[smtp]\n")


def test_missing_table_is_appended() -> None:
    p = _tmpdir() / "dot.toml"
    config.ensure_file(p)
    config.save_values(p, {"custom": {"answer": 42}})
    assert tomllib.loads(p.read_text(encoding="utf-8"))["custom"]["answer"] == 42


def test_values_with_quotes_and_newlines_roundtrip() -> None:
    p = _tmpdir() / "dot.toml"
    config.ensure_file(p)
    weird = '他说:"你好"\n第二行 \\ end'
    config.save_values(p, {"custom": {"weird": weird}})
    assert tomllib.loads(p.read_text(encoding="utf-8"))["custom"]["weird"] == weird


def test_bad_type_does_not_touch_file() -> None:
    p = _tmpdir() / "dot.toml"
    config.ensure_file(p)
    before = p.read_bytes()
    try:
        config.save_values(p, {"model": {"x": {"nested": 1}}})
    except TypeError:
        pass
    else:
        raise AssertionError("嵌套 dict 应该被拒")
    assert p.read_bytes() == before


def test_secrets_roundtrip_and_env_precedence() -> None:
    p = _tmpdir() / "secrets.env"
    config.write_secrets(p, {"A_KEY": "a", 'B_KEY': '带 "引号" 的值'})
    assert config.read_secrets(p) == {"A_KEY": "a", "B_KEY": '带 "引号" 的值'}

    config.write_secrets(p, {"A_KEY": ""})
    assert "A_KEY" not in config.read_secrets(p)

    old = os.environ.get("B_KEY")
    os.environ["B_KEY"] = "外部设置"
    try:
        config.load_secrets(p)
        assert os.environ["B_KEY"] == "外部设置"       # setdefault 不覆盖
    finally:
        if old is None:
            os.environ.pop("B_KEY", None)
        else:
            os.environ["B_KEY"] = old


def test_secret_rejects_newline() -> None:
    p = _tmpdir() / "secrets.env"
    try:
        config.write_secrets(p, {"K": "a\nb"})
    except ValueError:
        return
    raise AssertionError("换行应该被拒")
