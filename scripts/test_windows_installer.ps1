# Windows 发布门禁：验证安装包内容、静默安装、真实启动、后端健康、CORS 预检、退出回收、静默卸载。
# 只在 Windows CI 或 Windows 开发机执行；不会触碰用户默认数据目录。
param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$Installer
)

$ErrorActionPreference = "Stop"
if (-not $env:CI) {
  throw "该脚本会清理本机安装目录，只允许在 CI 中运行（先设置 CI=true）"
}
if ([string]::IsNullOrWhiteSpace($Installer) -or -not (Test-Path -LiteralPath $Installer)) {
  throw "安装包路径无效：$Installer"
}
$installerPath = (Get-Item -LiteralPath $Installer).FullName
$requireSignature = $env:REQUIRE_SIGNATURE -eq "true"
$product = "mangatrans"
$installDir = Join-Path $env:LOCALAPPDATA $product
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$workDir = Join-Path $tempRoot ("mangatrans-installer-smoke-" + [guid]::NewGuid().ToString("N"))
$dataDir = Join-Path $workDir "data"
$installerLog = Join-Path $workDir "installer.log"
$archiveLog = Join-Path $workDir "archive.log"
$selfCheckLog = Join-Path $tempRoot "mangatrans-install-check.log"
New-Item -ItemType Directory -Force -Path $workDir, $dataDir | Out-Null

function Get-SidecarLogTail {
  $log = Join-Path $dataDir "logs\sidecar.log"
  if (Test-Path $log) {
    return (Get-Content $log -Tail 80 -ErrorAction SilentlyContinue | Out-String)
  }
  return "（sidecar.log 不存在）"
}

function Stop-TestProcessTree {
  param([System.Diagnostics.Process]$Process)
  if ($Process -and -not $Process.HasExited) {
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  }
}

$appProcess = $null
$oldDataDir = $env:MT_DATA_DIR
try {
  # 1) 安装包自身必须是可读取的 NSIS 归档，且包含两个关键 exe。
  $sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source
  if (-not $sevenZip) {
    $candidate = Join-Path $env:ProgramFiles "7-Zip\7z.exe"
    if (Test-Path $candidate) { $sevenZip = $candidate }
  }
  if (-not $sevenZip) {
    throw "未找到 7z.exe，无法执行安装包完整性检查"
  }
  & $sevenZip t $installerPath *> $archiveLog
  if ($LASTEXITCODE -ne 0) {
    throw "7-Zip 无法校验安装包，可能已损坏：$installerPath`n$(Get-Content $archiveLog -Tail 40 | Out-String)"
  }
  & $sevenZip l -ba $installerPath *>> $archiveLog
  $listing = Get-Content $archiveLog -Raw
  foreach ($name in @("mangatrans.exe", "mangatrans-backend.exe")) {
    if ($listing -notmatch [regex]::Escape($name)) {
      throw "安装包内缺少 $name"
    }
  }
  Write-Host "安装包归档校验通过：主程序与后端均在包内"

  $setupSig = Get-AuthenticodeSignature -FilePath $installerPath
  if ($requireSignature -and -not $setupSig.SignerCertificate) {
    throw "安装包未签名或签名无效：$installerPath（status=$($setupSig.Status)）"
  }
  Write-Host "安装包签名：$($setupSig.Status) | 签名者=$($setupSig.SignerCertificate.Subject)"

  # 2) 清掉 runner 上可能存在的旧安装，确保测试从干净状态开始。
  $oldUninstaller = Join-Path $installDir "uninstall.exe"
  if (Test-Path $oldUninstaller) {
    $p = Start-Process -FilePath $oldUninstaller -ArgumentList "/S" -PassThru -Wait
    Start-Sleep -Seconds 1
  }
  if (Test-Path $installDir) {
    Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $selfCheckLog -Force -ErrorAction SilentlyContinue

  # 3) 静默安装，捕获真实退出码；0x2 必须暴露出来而不是被忽略。
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $installerPath
  $psi.Arguments = "/S"
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $installerProcess = [System.Diagnostics.Process]::Start($psi)
  if (-not $installerProcess.WaitForExit(180000)) {
    Stop-TestProcessTree $installerProcess
    throw "安装器 180 秒内未结束"
  }
  $installerOut = $installerProcess.StandardOutput.ReadToEnd()
  $installerErr = $installerProcess.StandardError.ReadToEnd()
  Set-Content -Encoding UTF8 -Path $installerLog -Value @($installerOut, $installerErr)
  if ($installerProcess.ExitCode -ne 0) {
    $selfCheck = if (Test-Path $selfCheckLog) { Get-Content $selfCheckLog -Raw } else { "（没有安装后自检日志）" }
    throw "静默安装失败（退出码 $($installerProcess.ExitCode)）。`n$selfCheck`n$(Get-Content $installerLog -Raw)"
  }
  Write-Host "静默安装完成（退出码 0）"

  # 4) 安装结果必须真实存在，签名状态可诊断。
  $appExe = Join-Path $installDir "mangatrans.exe"
  $backendExe = Join-Path $installDir "mangatrans-backend.exe"
  foreach ($exe in @($appExe, $backendExe)) {
    if (-not (Test-Path $exe)) { throw "安装后缺少文件：$exe" }
    if ((Get-Item $exe).Length -lt 1MB) { throw "安装后文件体积异常：$exe" }
    $sig = Get-AuthenticodeSignature -FilePath $exe
    if ($requireSignature -and -not $sig.SignerCertificate) {
      throw "安装后的文件未签名：$exe（status=$($sig.Status)）"
    }
    Write-Host "安装文件：$exe；大小=$((Get-Item $exe).Length)；签名状态=$($sig.Status)；签名者=$($sig.SignerCertificate.Subject)"
  }

  # 5) 用隔离数据目录启动真实桌面应用，等待 sidecar 写入端口与令牌。
  $env:MT_DATA_DIR = $dataDir
  $appProcess = Start-Process -FilePath $appExe -PassThru
  $lockPath = Join-Path $dataDir "backend.lock"
  $lock = $null
  $health = $null
  for ($i = 0; $i -lt 180; $i++) {
    Start-Sleep -Milliseconds 500
    $appProcess.Refresh()
    if ($appProcess.HasExited) {
      throw "应用启动后提前退出（code=$($appProcess.ExitCode)）。`n$(Get-SidecarLogTail)"
    }
    if (Test-Path $lockPath) {
      try {
        $candidate = Get-Content $lockPath -Raw | ConvertFrom-Json
        $port = [int]$candidate.port
        if ($port -gt 0) {
          $health = Invoke-RestMethod -TimeoutSec 2 -Uri "http://127.0.0.1:$port/health"
          if ($health.status -eq "ok") { $lock = $candidate; break }
        }
      } catch {
        # sidecar 仍在初始化，继续等待
      }
    }
  }
  if (-not $lock) {
    throw "90 秒内未收到 sidecar 健康响应。`n$(Get-SidecarLogTail)"
  }
  if (-not (Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue)) {
    throw "backend.lock 指向的后端进程不存在：pid=$($lock.pid)"
  }
  Write-Host "桌面应用已启动，sidecar 健康：port=$($lock.port)"

  $cors = Invoke-WebRequest -UseBasicParsing -Method Options -TimeoutSec 5 `
    -Headers @{
      "Origin" = "http://tauri.localhost"
      "Access-Control-Request-Method" = "GET"
    } -Uri "http://127.0.0.1:$($lock.port)/api/v1/projects"
  if ($cors.StatusCode -ne 200) {
    throw "CORS 预检返回 $($cors.StatusCode)"
  }
  $allowOrigin = $cors.Headers["Access-Control-Allow-Origin"]
  if ($allowOrigin -ne "http://tauri.localhost") {
    throw "CORS 预检未放行 Tauri v2 Origin，实际=$allowOrigin"
  }
  Write-Host "CORS 预检通过：http://tauri.localhost"

  # 6) 正常关闭应用后，后端必须被回收，不能留下孤儿进程。
  $null = $appProcess.CloseMainWindow()
  if (-not $appProcess.WaitForExit(20000)) {
    throw "关闭主窗口后应用 20 秒内未退出"
  }
  Start-Sleep -Seconds 3
  $leftover = Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue
  if ($leftover) {
    Stop-Process -Id $leftover.Id -Force -ErrorAction SilentlyContinue
    throw "应用退出后后端 sidecar 仍残留，pid=$($leftover.Id)"
  }
  Write-Host "应用退出后 sidecar 已正确回收"

  # 7) 静默卸载，确保卸载器可用。
  $uninstaller = Join-Path $installDir "uninstall.exe"
  if (-not (Test-Path $uninstaller)) { throw "安装目录缺少 uninstall.exe" }
  $u = Start-Process -FilePath $uninstaller -ArgumentList "/S" -PassThru -Wait
  if ($u.ExitCode -ne 0) { throw "静默卸载失败（退出码 $($u.ExitCode)）" }
  Start-Sleep -Seconds 2
  if (Test-Path $appExe) { throw "卸载后主程序仍存在：$appExe" }
  Write-Host "静默卸载通过"
} finally {
  if ($appProcess) { Stop-TestProcessTree $appProcess }
  if ($oldDataDir) { $env:MT_DATA_DIR = $oldDataDir } else { Remove-Item Env:MT_DATA_DIR -ErrorAction SilentlyContinue }
  Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
}
