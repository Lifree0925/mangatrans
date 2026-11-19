# 构建前端静态资源（Windows runner 上执行）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\frontend")

pnpm install --frozen-lockfile
pnpm build
Write-Host "frontend built: frontend\dist"
