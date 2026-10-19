"""交互终端(`dots` 不带子命令)。

测的不是「提示符好不好看」,是**一行出错不能把整个终端带走** ——
打错一个命令、配置文件不在了、引号没配对,都得回到 `dots>` 接着用。
交互终端要是脆,那还不如没有。

    python -m pytest tests/test_cli.py -q
    python -m tests.test_cli
"""

from __future__ import annotations

import builtins
import contextlib
import io
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage

from dots import cli
from dots import config as config_mod


def _repl(lines: list[str], args: list[str] = ()) -> tuple[str, int]:
    """喂几行给交互终端,把标准输出和退出码拿回来。

    自己顶掉 input,不碰真 stdin —— 自检要在管道里跑。
    """
    it = iter(lines)

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        try:
            return next(it)
        except StopIteration:      # 行喂完了 = Ctrl-D
            raise EOFError

    buf = io.StringIO()
    real = builtins.input
    builtins.input = fake_input
    try:
        # stderr 也要接住:argparse 的报错走的是它,漏了就看不到「打错命令会怎样」
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(list(args))
    finally:
        builtins.input = real
    return buf.getvalue(), code


@contextlib.contextmanager
def _sandbox():
    """空目录当工作目录,里面现生成一份 dot.toml。"""
    with tempfile.TemporaryDirectory() as td:
        with contextlib.chdir(td):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["init"])
            yield Path(td)


def test_dispatches_like_the_real_cli() -> str:
    """交互里下的目标,和 `dots goal` 下的是同一条 —— 走的是同一个 main()。"""
    with _sandbox():
        out, code = _repl(["/goal 把 inbox 整理成报告", "/ls", "/exit"])
    assert code == 0, code
    # 出现两次:goal 的回显一次,ls 从库里读出来一次 —— 证明真落库了,不是回显
    assert out.count("把 inbox 整理成报告") >= 2, out
    assert "ready" in out, out
    return "  交互终端下目标 / ls,和命令行一致  ✓"


def test_bad_line_does_not_kill_the_session() -> str:
    """打错命令、引号没配对,都得回到 dots> 接着用。"""
    with _sandbox():
        out, code = _repl(["/goat", '/goal "引号没关', "/cost", "/exit"])
    assert code == 0, code
    assert "invalid choice" in out, out
    assert "读不懂这行" in out, out
    assert "上限" in out, "出错之后没回到提示符,cost 没跑成"
    return "  坏输入只报错、不退出终端  ✓"


def test_missing_config_still_lands_in_the_repl() -> str:
    """没 init 也不能崩 —— 得进终端,并且告诉你去 init。"""
    with tempfile.TemporaryDirectory() as td:
        with contextlib.chdir(td):
            out, code = _repl(["/ls"], args=["-c", "nope.toml"])
    assert code == 0, code
    assert "没找到配置" in out, out
    return "  没有 dot.toml 时给提示而不是崩  ✓"


@contextlib.contextmanager
def _fake_home():
    """把全局配置挪到临时目录里,别动真 ~/.dots。"""
    real = config_mod.GLOBAL_PATH
    with tempfile.TemporaryDirectory() as home:
        config_mod.GLOBAL_PATH = Path(home) / ".dots" / "dot.toml"
        try:
            yield Path(home)
        finally:
            config_mod.GLOBAL_PATH = real


def test_global_config_works_from_anywhere() -> str:
    """init --global 之后,cd 到任何地方都不该再要你 init。"""
    with _fake_home():
        with tempfile.TemporaryDirectory() as work:
            out, code = _repl(["/exit"], args=["init", "--global"])
            assert code == 0 and config_mod.GLOBAL_PATH.exists(), out

            with contextlib.chdir(work):
                out, code = _repl(["/goal 出门在外也能下目标", "/ls", "/exit"])
                assert code == 0, code
                # 出现两次 = 真落库了,不是回显
                assert out.count("出门在外也能下目标") >= 2, out
                # 数据跟着全局配置走,不落在当前目录
                assert not Path(work, "dot.db").exists(), "数据写到当前目录了"
                assert not Path(work, "workspace").exists(), "workspace 写到当前目录了"
    return "  全局配置:换个目录不用 init 也能用  ✓"


def test_explicit_config_does_not_fall_back() -> str:
    """-c 打错字必须报错。

    默默兜底到全局那份才是坑:你以为在看 A,其实在看 B。
    """
    with _fake_home() as home:
        with tempfile.TemporaryDirectory() as work:
            with contextlib.chdir(home):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["init", "--global"])
            with contextlib.chdir(work):
                out, code = _repl(["/ls", "/exit"], args=["-c", "tyop.toml"])
            assert "找不到配置文件" in out, out
    return "  -c 打错字直接报错,不偷偷兜底  ✓"


def test_banner_is_aligned_and_stays_out_of_pipes() -> str:
    """手工粘的大字符最容易错位一格 —— 每行必须等宽。"""
    lines = cli.banner().splitlines()
    assert len(lines) == 6, f"banner 应该是 6 行,现在是 {len(lines)}"
    assert len({len(x) for x in lines}) == 1, [len(x) for x in lines]

    # 非终端(管道 / 重定向)不该吐方块字,不然日志里全是乱码
    with _sandbox():
        out, _ = _repl(["/exit"])
    assert "█" not in out, "banner 漏进非终端输出了"
    return "  banner 等宽,且只在真终端里出现  ✓"


class _FakeAgent:
    """只验「哪句话走了哪条路」,不碰真模型。

    真跑一轮要花几万 token,而这里要断言的只是路由:裸文本进 agent,斜杠进命令表。
    """

    def __init__(self, seen: list):
        self.seen = seen

    def get_state(self, config):
        return {"messages": []}

    def invoke(self, payload, config):
        self.seen.append(payload)
        return {"messages": [AIMessage("收到了")]}


class _FakeSaver:
    class conn:                      # noqa: N801 —— 只为了 .close() 能调
        @staticmethod
        def close():
            pass


def test_plain_text_goes_to_the_agent_not_the_parser() -> str:
    """裸文本是「跟它说话」,不是命令 —— 这是这版交互的全部新意。"""
    # 补丁打在 dots.worker 模块本身上,不打在 cli 上:`cli` 现在是惰性导入
    # worker(顶层导会把 langchain 连累进来),模块上根本没有 worker 这个属性。
    # 打在模块对象上,`_new_chat` 里那句 `from dots import worker` 拿到的
    # 就是同一个对象,照样打中。
    from dots import worker as worker_mod

    seen: list = []
    real = worker_mod.chat_agents
    worker_mod.chat_agents = lambda cfg, conn: ({"dot": _FakeAgent(seen)}, _FakeSaver())
    try:
        with _sandbox():
            out, code = _repl(["把 inbox 整理一下", "/ls", "再来一句"])
    finally:
        worker_mod.chat_agents = real

    assert code == 0, code
    said = [p["messages"][0].content for p in seen if isinstance(p, dict)]
    assert said == ["把 inbox 整理一下", "再来一句"], said
    assert out.count("收到了") == 2, out
    assert "还没有目标" in out, "/ls 没跑起来 —— 斜杠没被当成命令"
    return "  裸文本给 agent,斜杠还是命令  ✓"


def test_eof_exits_clean() -> str:
    """Ctrl-D 要干净退出,不能抛栈。"""
    with _sandbox():
        out, code = _repl([])
    assert code == 0, code
    return "  Ctrl-D 干净退出  ✓"


def run() -> str:
    cases = [
        test_dispatches_like_the_real_cli,
        test_bad_line_does_not_kill_the_session,
        test_missing_config_still_lands_in_the_repl,
        test_global_config_works_from_anywhere,
        test_explicit_config_does_not_fall_back,
        test_plain_text_goes_to_the_agent_not_the_parser,
        test_banner_is_aligned_and_stays_out_of_pipes,
        test_eof_exits_clean,
    ]
    return "\n".join(case() for case in cases)


def test_all_cli():
    run()


if __name__ == "__main__":
    print(run())
