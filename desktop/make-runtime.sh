#!/usr/bin/env bash
# 铺一份 desktop/runtime/ —— 打包时随安装包带走的 Python 运行时。
#
# 为什么不是 PyInstaller:这套依赖(deepagents / langchain / langgraph)在运行时
# 按名字动态加载 provider 包,PyInstaller 的 import 分析很容易漏,漏了就是
# 「装完能用,一换模型就崩」。这里干脆带一份能跑的完整环境,不做冻结。
#
# 产出(app 启动时按 resources/runtime/python/python.exe 找):
#   runtime/python/          嵌入式 Python(约 10 MB)
#   runtime/site-packages/   依赖 + dots 包本身(约 140 MB)
#
# 用法:在 desktop/ 下 bash make-runtime.sh
# 之后 npx electron-builder --win 就会把它塞进 resources/runtime/。
set -euo pipefail

PY_VER="${PY_VER:-3.13.9}"
MIRROR="${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
# python.org 在国内不稳,用华为云的镜像
PY_URL="https://mirrors.huaweicloud.com/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"

cd "$(dirname "$0")"
rm -rf runtime
mkdir -p runtime/python
cd runtime

echo "==> 下载嵌入式 Python ${PY_VER}"
curl -sL --max-time 300 -o py.zip "$PY_URL"

echo "==> 解压"
python - <<'PY'
import zipfile
zipfile.ZipFile("py.zip").extractall("python")
PY
rm -f py.zip

echo "==> 配 ._pth"
# 嵌入式 Python 一旦存在 ._pth,PYTHONPATH 会被忽略 —— 所以依赖目录必须写进这里,
# 靠环境变量传是没用的。这是这套东西最容易踩的坑。
python - <<'PY'
import pathlib, sys
tag = f"python{sys.version_info.major}{sys.version_info.minor}"
p = pathlib.Path("python") / f"{tag}._pth"
p.write_text(f"{tag}.zip\n.\n..\\site-packages\n", encoding="utf-8")
print("  写好", p)
PY

echo "==> 装依赖(镜像:$MIRROR)"
# 用系统的 pip 装到 --target:纯 Python 包 + cp313 的 win 轮子,
# 和嵌入式解释器同一个小版本,直接能用。
python -m pip install --quiet --target site-packages \
  "deepagents>=0.6" \
  "langgraph-checkpoint-sqlite>=2.0" \
  "python-dateutil>=2.8" \
  "langchain-openai>=0.3" \
  -i "$MIRROR"

echo "==> 把 dots 包本身放进去"
python - <<'PY'
import shutil, pathlib
src = pathlib.Path("../../dots")
dst = pathlib.Path("site-packages/dots")
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
print("  ->", dst)
PY

echo "==> 自检"
./python/python.exe -c "import dots.bridge, deepagents, langchain_openai, yaml; print('  运行时可用')"

du -sh . 2>/dev/null || true
echo "==> 完成。现在可以 npx electron-builder --win"
