# 本地开发内循环（Windows）：后端 --reload + 前端 Vite HMR
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

if (-not (Test-Path "$root\backend\.venv")) {
    Write-Host "初始化 Python 虚拟环境…"
    python -m venv "$root\backend\.venv"
    & "$root\backend\.venv\Scripts\pip.exe" install -q -e "$root\backend[dev]"
}

$backendCmd = @"
cd '$root\backend'
`$env:MT_AUTH_TOKEN = 'dev-token-local'
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8765 --reload
"@

$frontendCmd = @"
cd '$root\frontend'
pnpm dev
"@

Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

Write-Host "前端 http://localhost:5173 · 后端 http://127.0.0.1:8765"
Write-Host "关闭两个 PowerShell 窗口即可停止。"
