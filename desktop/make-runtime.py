#!/usr/bin/env python
"""铺一份 desktop/runtime/ —— 打包时随安装包带走的 Python 运行时。

和 make-runtime.sh 做同样的事,只是不依赖 bash/curl:
Windows 上默认没有 Bash,而 curl 走 schannel 在受限环境里还会报
SEC_E_NO_CREDENTIALS。用当前 Python 下载 + 解压 + pip --target,到处都能跑。

产出(app 启动时按 resources/runtime/python/python.exe 找):
  runtime/python/          嵌入式 Python(约 10 MB)
  runtime/site-packages/   依赖 + dots 包本身(约 140 MB)

用法(在 desktop/ 下):
  python make-runtime.py
之后 `npm run dist` 就会把它塞进 resources/runtime/。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PY_VER = os.environ.get("PY_VER", "3.13.9")
MIRROR = os.environ.get("MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple")
# python.org 在国内不稳,用华为云镜像
PY_URL = os.environ.get(
    "PY_URL",
    f"https://mirrors.huaweicloud.com/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip",
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RUNTIME = HERE / "runtime"
DEPS = [
    "deepagents>=0.6",
    "langgraph-checkpoint-sqlite>=2.0",
    "python-dateutil>=2.8",
    "langchain-openai>=0.3",
    # dots 自己直接 import 的
    "pyyaml>=6",
]


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "dots-build"})
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def main() -> int:
    tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    # 嵌入式解释器的小版本决定 ._pth 文件名,和打包机的 Python 版本无关 ——
    # 这里按下载的版本推:3.13.9 -> python313._pth
    minor = ".".join(PY_VER.split(".")[:2]).replace(".", "")
    pth_name = f"python{minor}._pth"

    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    (RUNTIME / "python").mkdir(parents=True)

    step(f"下载嵌入式 Python {PY_VER}")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "py.zip"
        download(PY_URL, zip_path)
        step("解压")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(RUNTIME / "python")

    step("配 ._pth")
    # 嵌入式 Python 一旦存在 ._pth,PYTHONPATH 会被忽略 —— 依赖目录必须写进这里,
    # 靠环境变量传是没用的。这是这套东西最容易踩的坑。
    (RUNTIME / "python" / pth_name).write_text(
        f"python{minor}.zip\n.\n..\\site-packages\n", encoding="utf-8")
    print("  写好", RUNTIME / "python" / pth_name)

    step(f"装依赖(镜像:{MIRROR})")
    # 优先用 uv:解析快、不会在依赖回溯上空转(pip 在 deepagents 这棵树上能跑十几分钟)。
    # uv 必须显式指定嵌入式解释器,否则它可能按宿主 Python 选错 wheel
    # (踩过:装进来的 pydantic_core 是 cp312 的,导入时报 unknown location)。
    exe = RUNTIME / "python" / "python.exe"
    uv = shutil.which("uv")
    env = dict(os.environ)
    # pip 解 wheel 时用的是 tempfile,受限环境里 0700 的临时目录会 permission denied;
    # 指到工作区里的普通目录就绕开了。
    Path(REPO / ".tmp-build").mkdir(parents=True, exist_ok=True)
    env.setdefault("TMPDIR", str(REPO / ".tmp-build"))
    env.setdefault("TEMP", str(REPO / ".tmp-build"))
    env.setdefault("TMP", str(REPO / ".tmp-build"))

    if uv:
        cmd = [uv, "pip", "install", "--python", str(exe),
               "--target", str(RUNTIME / "site-packages"),
               "--index-url", MIRROR, "--only-binary", ":all:", *DEPS]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
               "--target", str(RUNTIME / "site-packages"), *DEPS, "-i", MIRROR,
               "--only-binary", ":all:", "--disable-pip-version-check",
               "--no-warn-script-location"]
    subprocess.run(cmd, check=True, env=env)

    step("把 dots 包本身放进去")
    dst = RUNTIME / "site-packages" / "dots"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(REPO / "dots", dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("  ->", dst)

    step("自检")
    exe = RUNTIME / "python" / "python.exe"
    proc = subprocess.run(
        [str(exe), "-c",
         "import dots.bridge, deepagents, langchain_openai, yaml; print('  运行时可用')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(proc.stdout.strip() or proc.stderr.strip()[-800:])
    if proc.returncode != 0:
        return proc.returncode

    size = sum(f.stat().st_size for f in RUNTIME.rglob("*") if f.is_file())
    step(f"完成,runtime 约 {size / 1024 / 1024:.0f} MB。现在可以 npm run dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
