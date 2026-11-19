# -*- mode: python ; coding: utf-8 -*-
# PyInstaller one-folder 构建（后续验证后再评估 one-file）
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("sqlalchemy.dialects.sqlite") + [
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mangatrans-backend",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mangatrans-backend",
)
