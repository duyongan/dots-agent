"""白名单规则。纯函数、零 IO —— 全项目唯一值得单测的地方。

规则错了后果是双向的:要么它偷偷干了不该干的,要么什么都干不了。
所以这里默认从严:没显式配的一律 ask。
"""

from __future__ import annotations

from fnmatch import fnmatchcase

ALLOW = "allow"   # 白名单,直接执行
ASK = "ask"       # 挂起,发邮件等人回
DENY = "deny"     # 永不执行

VALID = (ALLOW, ASK, DENY)
_WILDCARD = "*?["


def decide(action: str, rules: dict[str, str]) -> str:
    """精确名 > 通配 > 默认 ASK。

    通配命中多个时取最长的那个 —— 越长越具体。
    """
    if action in rules:
        return _norm(rules[action], action)
    matches = [p for p in rules if any(c in p for c in _WILDCARD) and fnmatchcase(action, p)]
    if matches:
        return _norm(rules[max(matches, key=len)], action)
    return ASK


def validate(rules: dict[str, str]) -> list[str]:
    """启动时跑一遍,把配置错误变成一句人话,而不是跑到一半抛异常。"""
    problems = []
    for pattern, value in rules.items():
        if value.strip().lower() not in VALID:
            problems.append(
                f"[approval.rules] {pattern} = {value!r} 不合法,只能是 allow / ask / deny"
            )
    return problems


def _norm(value: str, action: str) -> str:
    v = value.strip().lower()
    if v not in VALID:
        raise ValueError(
            f"规则 {action!r} 的值 {value!r} 不合法,只能是 allow / ask / deny"
        )
    return v


def demo() -> None:
    """python -m dots.rules 直接跑这个。"""
    r = {"send_email": "ask", "run_shell": "deny", "fs_*": "allow"}
    assert decide("send_email", r) == ASK
    assert decide("run_shell", r) == DENY
    assert decide("fs_write", r) == ALLOW
    assert decide("fs_read", r) == ALLOW
    assert decide("rm_rf", r) == ASK, "没配到的必须默认从严"
    # 精确名压过通配
    assert decide("fs_delete", {"fs_*": "allow", "fs_delete": "ask"}) == ASK
    # 通配命中多个取最长的
    assert decide("fs_delete", {"fs_*": "allow", "fs_d*": "deny"}) == DENY
    assert validate({"a": "nope"}) != []
    assert validate({"a": "allow"}) == []
    # 大小写和空格要容错,人会手抖
    assert decide("x", {"x": " ALLOW "}) == ALLOW
    print("rules: ok")


if __name__ == "__main__":
    demo()
