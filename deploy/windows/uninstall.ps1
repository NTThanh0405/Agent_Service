# deploy\windows\uninstall.ps1 — gỡ Windows Service (giữ nguyên dữ liệu trong workspaces\ và logs\).
#   powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 [-ServiceName Bizagent]
param([string]$ServiceName = "Bizagent")

$ErrorActionPreference = "Stop"
$Exe = Join-Path $PSScriptRoot "service\$ServiceName.exe"
if (-not (Test-Path $Exe)) { throw "Không thấy $Exe" }
& $Exe stop
& $Exe uninstall
Unregister-ScheduledTask -TaskName "$ServiceName-Health" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Đã gỡ service $ServiceName. Dữ liệu (workspaces\, logs\, .env) vẫn còn."
