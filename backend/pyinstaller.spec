# -*- mode: python ; coding: utf-8 -*-
# PyInstaller one-folder 构建（后续验证后再评估 one-file）
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("sqlalchemy.dialects.sqlite") + [
    "multipart",
    "python_multipart",
    "docx",
    "httpx",
    "keyring",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["app/main.py"],
    pathex=[],
    binaries=[],
    datas=[("migrations", "migrations"), ("alembic.ini", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# one-file：单文件自包含，直接作为 Tauri externalBin 分发（one-folder 的 _internal 无法随 externalBin 打包）
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mangatrans-backend",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
