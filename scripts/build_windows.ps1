# 完整 Windows 发布构建：sidecar → 前端 → Tauri bundle
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

& (Join-Path $PSScriptRoot "build_backend.ps1")
& (Join-Path $PSScriptRoot "build_frontend.ps1")

# 将 sidecar 放入 Tauri externalBin 约定目录（文件名含 target triple）
$targetTriple = "x86_64-pc-windows-msvc"
$binDir = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Copy-Item (Join-Path $root "backend\dist\mangatrans-backend\mangatrans-backend.exe") `
          (Join-Path $binDir "mangatrans-backend-$targetTriple.exe") -Force

Set-Location (Join-Path $root "src-tauri")
cargo tauri build
Write-Host "installers: src-tauri\target\release\bundle"
