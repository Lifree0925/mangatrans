# 完整 Windows 发布构建：sidecar → 前端 → Tauri bundle
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

& (Join-Path $PSScriptRoot "build_backend.ps1")
& (Join-Path $PSScriptRoot "build_frontend.ps1")

# PyInstaller 当前使用 one-file，产物是单个 exe；不要沿用旧 one-folder 路径。
$backendExe = Join-Path $root "backend\dist\mangatrans-backend.exe"
if (-not (Test-Path $backendExe)) {
  throw "后端 sidecar 不存在：$backendExe（请检查 PyInstaller one-file 构建是否成功）"
}
if ((Get-Item $backendExe).Length -lt 10MB) {
  throw "后端 sidecar 体积异常，可能构建不完整：$backendExe"
}

$targetTriple = "x86_64-pc-windows-msvc"
$binDir = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$sidecar = Join-Path $binDir "mangatrans-backend-$targetTriple.exe"
Copy-Item $backendExe $sidecar -Force
if (-not (Test-Path $sidecar)) {
  throw "sidecar 复制失败：$sidecar"
}

# 使用前端本地 @tauri-apps/cli，避免依赖未安装的 cargo-tauri。
$tauriCli = Join-Path $root "frontend\node_modules\.bin\tauri.cmd"
if (-not (Test-Path $tauriCli)) {
  throw "缺少 Tauri CLI：$tauriCli（先在 frontend 执行 pnpm install）"
}
if (-not $env:TAURI_SIGNING_PRIVATE_KEY -or -not $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD) {
  throw "缺少 Tauri updater 签名环境变量：TAURI_SIGNING_PRIVATE_KEY / TAURI_SIGNING_PRIVATE_KEY_PASSWORD"
}

Set-Location (Join-Path $root "src-tauri")
$tauriArgs = @("build")
if ($env:PFX_B64 -and $env:PFX_PASS) {
  $tauriArgs += @("--config", "tauri.signing.conf.json")
}
& $tauriCli @tauriArgs
if ($LASTEXITCODE -ne 0) {
  throw "Tauri build 失败，退出码 $LASTEXITCODE"
}

$installer = Get-ChildItem (Join-Path $root "src-tauri\target\release\bundle\nsis\*.exe") -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $installer) {
  throw "Tauri build 结束但没有生成 NSIS 安装包"
}
Write-Host "installer: $($installer.FullName)"
