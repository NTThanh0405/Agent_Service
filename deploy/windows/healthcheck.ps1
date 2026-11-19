# deploy\windows\healthcheck.ps1 — khởi động lại service khi tiến trình còn sống nhưng không trả lời (treo).
# WinSW đã tự khởi động lại khi tiến trình CHẾT; script này lo trường hợp treo.
#
# Đăng ký chạy mỗi 5 phút (PowerShell Admin):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\healthcheck.ps1 -Register [-ServiceName Bizagent] [-Port 8000]
param(
    [string]$ServiceName = "Bizagent",
    [int]$Port = 8000,
    [switch]$Register
)

if ($Register) {
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -ServiceName $ServiceName -Port $Port"
    $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "$ServiceName-Health" -Action $a -Trigger $t -User "SYSTEM" -RunLevel Highest -Force | Out-Null
    Write-Host "Đã đăng ký tác vụ $ServiceName-Health (mỗi 5 phút)."
    exit 0
}

$Log = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path "logs\healthcheck.log"
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc -or $svc.Status -ne "Running") { exit 0 }      # đang dừng có chủ ý, hoặc WinSW đang khởi động lại

for ($i = 1; $i -le 3; $i++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 10 | Out-Null
        exit 0
    } catch {
        Start-Sleep -Seconds 10
    }
}
Add-Content -Path $Log -Value "$(Get-Date -Format s) health check thất bại 3 lần, khởi động lại $ServiceName"
Restart-Service -Name $ServiceName -Force
