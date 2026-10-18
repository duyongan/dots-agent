#!/usr/bin/env bash
# 一键出 Windows 安装包。在仓库根目录跑：bash packaging/build.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv-build/Scripts/python.exe"
[ -x "$PY" ] || { python -m venv .venv-build; "$PY" -m pip install -q -U pip pyinstaller; "$PY" -m pip install -q .; }

"$PY" -m PyInstaller --clean --noconfirm packaging/dots.spec
./.venv-build/Scripts/dots.exe selftest

for ISCC in \
  "C:/Users/$USERNAME/AppData/Local/Programs/Inno Setup 6/ISCC.exe" \
  "C:/Program Files (x86)/Inno Setup 6/ISCC.exe" \
  "C:/Program Files/Inno Setup 6/ISCC.exe"; do
  [ -x "$ISCC" ] && break
done
echo "==> $ISCC"
"$ISCC" packaging/dots.iss

ls -lh release/dots-setup-*.exe
