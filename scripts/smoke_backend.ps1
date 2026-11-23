# Windows 启动冒烟测试：全新数据目录实际拉起 sidecar，验证健康检查与单实例锁。
# 用法：pwsh -File scripts/smoke_backend.ps1 [-BackendPath <exe>]
param(
  [string]$BackendPath = "backend/dist/mangatrans-backend.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $BackendPath)) {
  throw "后端可执行文件不存在：$BackendPath"
}

# 选一个空闲的本机端口，避免与 runner 上其它进程冲突
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$dataDir = Join-Path $tempRoot ("mangatrans-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$stdout = Join-Path $dataDir "stdout.log"
$stderr = Join-Path $dataDir "stderr.log"
$token = "smoke-test-token"

$env:MT_PORT = "$port"
$env:MT_AUTH_TOKEN = $token
$env:MT_DATA_DIR = $dataDir
$env:MT_MIGRATE_ON_START = "1"

function Start-Backend {
  $p = Start-Process -FilePath $BackendPath -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
  return $p
}

function Wait-Healthy([System.Diagnostics.Process]$proc, [int]$attempts = 120) {
  for ($i = 0; $i -lt $attempts; $i++) {
    Start-Sleep -Milliseconds 500
    $proc.Refresh()
    if ($proc.HasExited) {
      throw "后端提前退出（code=$($proc.ExitCode)）。stderr：`n$(Get-Content $stderr -Raw -ErrorAction SilentlyContinue)"
    }
    try {
      $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 `
        -Uri "http://127.0.0.1:$port/health"
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) {
        Write-Host "backend healthy on port $port after $([math]::Ceiling(($i + 1) * 0.5))s"
        return
      }
    } catch {
      # 启动中，继续探测
    }
  }
  throw "后端 60 秒内未通过健康检查。stderr：`n$(Get-Content $stderr -Raw -ErrorAction SilentlyContinue)"
}

$first = Start-Backend
try {
  Wait-Healthy $first
  $health = Invoke-RestMethod -TimeoutSec 2 -Uri "http://127.0.0.1:$port/health"
  if ($health.status -ne "ok") {
    throw "健康检查返回异常：$($health | ConvertTo-Json -Compress)"
  }

  # 同一数据目录的第二实例必须退出，不能残留第二套 Worker
  # 输出重定向到独立文件，避免复用首个进程的 stdout 句柄
  $second = Start-Process -FilePath $BackendPath -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dataDir "second-stdout.log") `
    -RedirectStandardError (Join-Path $dataDir "second-stderr.log")
  $secondExited = $second.WaitForExit(30000)
  if (-not $secondExited) {
    Stop-Process -Id $second.Id -Force
    throw "单实例锁失效：第二实例 30 秒内未退出"
  }
  if ($second.ExitCode -ne 0) {
    throw "单实例锁失效：第二实例异常退出（code=$($second.ExitCode)），可能因端口冲突而非锁检测"
  }
  Write-Host "single-instance lock ok (second exit code=0)"

  # 第二实例退出后，首个后端仍必须可用
  $again = Invoke-RestMethod -TimeoutSec 2 -Uri "http://127.0.0.1:$port/health"
  if ($again.status -ne "ok") {
    throw "第二实例退出后首个后端不可用"
  }
  Write-Host "backend startup smoke test passed"
} finally {
  $first.Refresh()
  if (-not $first.HasExited) {
    Stop-Process -Id $first.Id -Force
  }
  Write-Host "--- backend stdout ---"
  Get-Content $stdout -Tail 40 -ErrorAction SilentlyContinue
  Write-Host "--- backend stderr ---"
  Get-Content $stderr -Tail 80 -ErrorAction SilentlyContinue
}
