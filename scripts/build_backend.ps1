# 构建 Python sidecar（Windows runner 上执行）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\backend")

python -m venv .venv-build
& .venv-build\Scripts\python.exe -m pip install --upgrade pip
& .venv-build\Scripts\python.exe -m pip install .
& .venv-build\Scripts\python.exe -m pip install pyinstaller
& .venv-build\Scripts\pyinstaller.exe pyinstaller.spec

# 产物位于 backend\dist\mangatrans-backend
Write-Host "sidecar built: backend\dist\mangatrans-backend"
