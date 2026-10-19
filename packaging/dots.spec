# -*- mode: python ; coding: utf-8 -*-
"""打包成 onedir 目录，再由 Inno Setup 装进用户目录。

onedir 而不是 onefile：安装包本来就会铺开目录，onefile 每次启动还要
解压一遍（langchain 这套依赖不小），纯亏。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))

# dots init 要读模板，frozen 后 __file__ 指向 _internal/dots/cli.py，
# 所以模板必须放在 dots/ 下面。
datas = [(os.path.join(root, "dots", "template.toml"), "dots")]
# React 构建产物（提交在 dots/webui），gui.py 按 __file__ 找它。
datas.append((os.path.join(root, "dots", "webui"), "dots/webui"))
# deepagents 的提示词/资源不是 .py，静态分析看不见。
datas += collect_data_files("deepagents")

hiddenimports = [
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "langgraph.checkpoint.sqlite",
    "langgraph.checkpoint.memory",
]
# checkpointer 是运行时按字符串找的（sqlite / memory），静态分析抓不到。
hiddenimports += collect_submodules("langgraph.checkpoint")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "IPython", "matplotlib", "pandas", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dots",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
# 同一个 Analysis/PYZ,只多一个无控制台的启动器:开始菜单双击不弹黑框。
gui_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dots-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    gui_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="dots",
)
