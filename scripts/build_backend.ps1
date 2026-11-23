# 构建 Python sidecar（Windows runner 上执行）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\backend")

python -m venv .venv-build
& .venv-build\Scripts\python.exe -m pip install --upgrade pip
& .venv-build\Scripts\python.exe -m pip install .
& .venv-build\Scripts\python.exe -m pip install pyinstaller
& .venv-build\Scripts\pyinstaller.exe pyinstaller.spec

$builtExe = Join-Path (Get-Location) "dist\mangatrans-backend.exe"
if (-not (Test-Path $builtExe)) {
  throw "PyInstaller 未生成 one-file sidecar：$builtExe"
}
$size = (Get-Item $builtExe).Length
if ($size -lt 10MB) {
  throw "PyInstaller sidecar 体积异常（$size bytes）：$builtExe"
}
Write-Host "sidecar built: $builtExe ($size bytes)"
