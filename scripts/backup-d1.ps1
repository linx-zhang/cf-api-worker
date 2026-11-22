# 只导出，不恢复：给 D1 留一份可离线打开的 SQL 副本。
#
# 安全边界写死在脚本里：
# * 只有 `wrangler d1 export`，没有 import / execute / migrations apply；
# * 目标文件已存在时直接失败，不覆盖旧备份；
# * 导出后做最小完整性检查（非空、含 CREATE TABLE），避免把空文件当成功。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup-d1.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/backup-d1.ps1 -Local
#
# `-Local` 只读本地 wrangler dev 的库，适合验证脚本；生产备份默认 `--remote`。

[CmdletBinding()]
param(
    [string]$OutputDir = "",
    [switch]$Local
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $repo "backups"
}
Set-Location -LiteralPath $repo

$wrangler = Join-Path $repo "node_modules\.bin\wrangler.cmd"
if (-not (Test-Path -LiteralPath $wrangler)) {
    throw "wrangler not found: $wrangler; run npm ci first"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$suffix = if ($Local) { "local" } else { "remote" }
$output = Join-Path $OutputDir "api-worker-db-$stamp-$suffix.sql"

if (Test-Path -LiteralPath $output) {
    throw "backup file already exists; refusing to overwrite: $output"
}

$exportArgs = @(
    "d1", "export", "api-worker-db",
    "--output", $output,
    "--skip-confirmation"
)
$exportArgs += if ($Local) { "--local" } else { "--remote" }

Write-Host "Exporting D1 ($suffix): $output"
& $wrangler @exportArgs
if ($LASTEXITCODE -ne 0) {
    throw "wrangler d1 export failed with exit code $LASTEXITCODE"
}

$file = Get-Item -LiteralPath $output
if ($file.Length -le 0) {
    throw "export is empty: $output"
}

$sql = Get-Content -LiteralPath $output -Raw
if ($sql -notmatch "(?m)^CREATE TABLE") {
    throw "export has no CREATE TABLE; refusing to call it a full backup: $output"
}

Write-Host ("Done: {0:N0} bytes; read-only export, no restore or write was run." -f $file.Length)
Write-Output $output
