# Tauri bundle.windows.signCommand 入口。
# 无签名 Secrets 时跳过，保证本地/无证书构建仍可打包；
# 一旦配置了 Secrets，任何一步签名失败都必须让构建失败，禁止静默发布未签名资产。
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
  throw "待签名文件不存在：$Path"
}

$pfxB64 = $env:PFX_B64
$pfxPass = $env:PFX_PASS
if (-not $pfxB64 -and -not $pfxPass) {
  Write-Host "未配置 Windows 签名证书，跳过签名：$Path"
  exit 0
}
if (-not $pfxB64 -or -not $pfxPass) {
  throw "签名配置不完整：PFX_B64 与 PFX_PASS 必须同时提供"
}

function Find-SignTool {
  $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }

  $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
  if (-not (Test-Path $kits)) {
    throw "找不到 signtool.exe：Windows SDK 未安装"
  }
  $tool = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
  if (-not $tool) {
    throw "找不到 x64 signtool.exe：$kits"
  }
  return $tool.FullName
}

$signtool = Find-SignTool
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$pfx = Join-Path $tempRoot ("mangatrans-sign-" + [guid]::NewGuid().ToString("N") + ".pfx")
$timestamp = if ($env:WINDOWS_SIGN_TIMESTAMP_URL) {
  $env:WINDOWS_SIGN_TIMESTAMP_URL
} else {
  "http://timestamp.digicert.com"
}

try {
  $cleanB64 = ($pfxB64 -replace '\s', '')
  [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($cleanB64))

  & $signtool sign /f $pfx /p $pfxPass /fd SHA256 `
    /tr $timestamp /td SHA256 /d "MangaTrans" $Path
  if ($LASTEXITCODE -ne 0) {
    throw "signtool 签名失败（退出码 $LASTEXITCODE）：$Path"
  }

  # 自签名证书不会通过公共信任链，但签名者证书必须存在；
  # 正式证书替换 Secrets 后，Windows 会显示受信任发布者。
  $check = Get-AuthenticodeSignature -FilePath $Path
  if (-not $check.SignerCertificate) {
    throw "签名校验失败：$Path（status=$($check.Status)）"
  }
  Write-Host "已签名：$Path（status=$($check.Status)）"
} finally {
  Remove-Item -LiteralPath $pfx -Force -ErrorAction SilentlyContinue
}
