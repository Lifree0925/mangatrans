# Windows 安装/启动诊断：只读检查，不修改数据、不结束进程。
# 用法：pwsh -File scripts/diagnose_windows.ps1 -Installer D:\path\mangatrans_0.1.0_x64-setup.exe
param(
  [string]$Installer = ""
)

$ErrorActionPreference = "Continue"
$installDir = Join-Path $env:LOCALAPPDATA "mangatrans"
Write-Host "== MangaTrans Windows 诊断 =="
Write-Host "时间：$(Get-Date -Format o)"
Write-Host "系统：$([Environment]::OSVersion.VersionString)"
Write-Host "安装目录：$installDir"

foreach ($name in @("mangatrans.exe", "mangatrans-backend.exe", "uninstall.exe")) {
  $path = Join-Path $installDir $name
  if (Test-Path $path) {
    $item = Get-Item $path
    $sig = Get-AuthenticodeSignature -FilePath $path
    Write-Host "文件：$name | 大小=$($item.Length) | 版本=$($item.VersionInfo.FileVersion) | 签名=$($sig.Status) | 签名者=$($sig.SignerCertificate.Subject)"
  } else {
    Write-Host "文件：$name | 缺失"
  }
}

$webview = $null
foreach ($key in @(
  "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
  "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
  "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)) {
  $v = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).pv
  if ($v) { $webview = $v; break }
}
Write-Host "WebView2：$(if ($webview) { $webview } else { '未检测到（安装器需要在线获取运行时）' })"

Write-Host "进程："
Get-Process -Name mangatrans, mangatrans-backend -ErrorAction SilentlyContinue |
  ForEach-Object { Write-Host "  pid=$($_.Id) name=$($_.ProcessName) path=$($_.Path)" }

$configPath = Join-Path $env:APPDATA "com.mangatrans.desktop\config.json"
Write-Host "桌面配置：$configPath"
if (Test-Path $configPath) { Get-Content $configPath -Raw | Write-Host }

$dataCandidates = @()
if ($env:MT_DATA_DIR) { $dataCandidates += $env:MT_DATA_DIR }
if (Test-Path $configPath) {
  try {
    $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
    if ($cfg.data_dir) { $dataCandidates += $cfg.data_dir }
  } catch {}
}
$dataCandidates += (Join-Path $env:APPDATA "com.mangatrans.desktop")
$dataCandidates += (Join-Path $env:LOCALAPPDATA "com.mangatrans.desktop")
$dataCandidates = $dataCandidates | Where-Object { $_ } | Select-Object -Unique

$lockPath = $null
foreach ($dir in $dataCandidates) {
  $candidate = Join-Path $dir "backend.lock"
  if (Test-Path $candidate) { $lockPath = $candidate; break }
}
if ($lockPath) {
  $dataDir = Split-Path $lockPath -Parent
  Write-Host "数据目录：$dataDir"
  $lockText = Get-Content $lockPath -Raw
  Write-Host "backend.lock：$lockText"
  try {
    $lock = $lockText | ConvertFrom-Json
    $port = [int]$lock.port
    if ($port -gt 0) {
      try {
        $health = Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$port/health"
        Write-Host "健康检查：status=$($health.status) version=$($health.version) port=$port"
      } catch {
        Write-Host "健康检查失败：$($_.Exception.Message)"
      }
      try {
        $cors = Invoke-WebRequest -UseBasicParsing -Method Options -TimeoutSec 3 `
          -Headers @{ "Origin" = "http://tauri.localhost"; "Access-Control-Request-Method" = "GET" } `
          -Uri "http://127.0.0.1:$port/api/v1/projects"
        Write-Host "CORS 预检：$($cors.StatusCode) allow-origin=$($cors.Headers['Access-Control-Allow-Origin'])"
      } catch {
        Write-Host "CORS 预检失败：$($_.Exception.Message)"
      }
    }
  } catch {
    Write-Host "backend.lock 解析失败：$($_.Exception.Message)"
  }
  $log = Join-Path $dataDir "logs\sidecar.log"
  if (Test-Path $log) {
    Write-Host "== sidecar.log 末尾 80 行 =="
    Get-Content $log -Tail 80
  } else {
    Write-Host "sidecar.log：缺失（$log）"
  }
} else {
  Write-Host "数据目录：未找到 backend.lock，后端可能从未成功启动"
}

$checkLog = Join-Path $TEMP "mangatrans-install-check.log"
Write-Host "安装自检日志：$checkLog"
if (Test-Path $checkLog) { Get-Content $checkLog -Raw | Write-Host }

if ($Installer -and (Test-Path $Installer)) {
  $item = Get-Item $Installer
  Write-Host "安装包：$($item.FullName)"
  Write-Host "安装包大小：$($item.Length)"
  Write-Host "安装包 SHA256：$((Get-FileHash $item.FullName -Algorithm SHA256).Hash)"
  $sig = Get-AuthenticodeSignature -FilePath $item.FullName
  Write-Host "安装包签名：$($sig.Status) | 签名者=$($sig.SignerCertificate.Subject)"
}
