# deploy\windows\update.ps1 — cập nhật code mới từ GitHub và khởi động lại service.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1 [-ServiceName Bizagent] [-Extras ...]
param(
    [string]$ServiceName = "Bizagent",
    [string]$Extras = "anthropic,telegram,postgres,mssql,excel,gsheets,mcp"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
git pull --ff-only
if ($LASTEXITCODE) { throw "git pull lỗi (có sửa đổi chưa commit?)" }
& "$Root\.venv\Scripts\python.exe" -m pip install -e ".[$Extras]"
if ($LASTEXITCODE) { throw "pip install lỗi" }
& "$Root\.venv\Scripts\python.exe" -m bizagent check
Restart-Service -Name $ServiceName
Write-Host "Đã cập nhật và khởi động lại $ServiceName."
