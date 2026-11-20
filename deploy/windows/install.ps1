# deploy\windows\install.ps1 — cài bizagent thành Windows Service (Windows Server 2019/2022/2025, Windows 10/11).
#
# Chạy trong PowerShell "Run as Administrator", tại thư mục gốc dự án:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -WinSW C:\tools\WinSW-x64.exe -Workspace cong_ty_a
#
# Script làm: tạo .venv + cài thư viện -> tạo .env (sinh sẵn khoá bí mật) nếu chưa có -> bizagent check
#   -> cấp quyền ghi workspaces\ và logs\ cho LocalService -> sinh file cấu hình WinSW -> cài + bật service.
# Thử trước không thay đổi gì: thêm -DryRun.

param(
    [string]$WinSW = "",                          # đường dẫn WinSW-x64.exe v2.12 (github.com/winsw/winsw/releases)
    [string]$Workspace = "demo",                  # tên thư mục trong workspaces\
    [string]$ServiceName = "Bizagent",
    [string]$ListenHost = "127.0.0.1",            # giữ 127.0.0.1 khi đặt sau Cloudflare Tunnel / IIS / nginx
    [int]$Port = 8000,
    [string]$Extras = "anthropic,telegram,postgres,mssql,excel,gsheets,mcp",
    [string]$PythonVersion = "3.12",              # bản Python cài trên máy (py launcher): 3.11 trở lên
    [switch]$SkipInstall,                         # không tạo venv / cài thư viện (đã làm rồi)
    [switch]$DryRun                               # chỉ in ra việc sẽ làm và file cấu hình service
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$SvcDir = Join-Path $PSScriptRoot "service"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$LocalService = "*S-1-5-19"                       # SID của NT AUTHORITY\LocalService (không phụ thuộc ngôn ngữ Windows)

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function New-Secret {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

if (-not $DryRun) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "Hãy mở PowerShell bằng 'Run as Administrator'." }
    if (-not $WinSW -or -not (Test-Path $WinSW)) { throw "Thiếu -WinSW <đường dẫn WinSW-x64.exe>." }
}
if (-not (Test-Path (Join-Path $Root "workspaces\$Workspace\workspace.yaml"))) {
    throw "Không thấy workspaces\$Workspace\workspace.yaml (tạo bằng: bizagent init $Workspace)."
}
Set-Location $Root

# 1. Python + thư viện -------------------------------------------------------------------------
if (-not $SkipInstall) {
    Step "Tạo .venv và cài bizagent[$Extras]"
    if (-not $DryRun) {
        if (-not (Test-Path $Py)) { & py "-$PythonVersion" -m venv .venv; if ($LASTEXITCODE) { throw "Không tạo được venv (đã cài Python $PythonVersion chưa?)" } }
        & $Py -m pip install --upgrade pip
        & $Py -m pip install -e ".[$Extras]"
        if ($LASTEXITCODE) { throw "pip install lỗi" }
    }
}

# 2. .env ------------------------------------------------------------------------------------
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Step "Tạo .env từ .env.example (sinh sẵn BIZAGENT_SECRET_KEY và BIZAGENT_API_TOKEN)"
    if (-not $DryRun) {
        $text = Get-Content (Join-Path $Root ".env.example") -Raw -Encoding UTF8
        $text = $text -replace "(?m)^BIZAGENT_SECRET_KEY=[ \t]*\r?$", "BIZAGENT_SECRET_KEY=$(New-Secret)"
        $text = $text -replace "(?m)^BIZAGENT_API_TOKEN=[ \t]*\r?$", "BIZAGENT_API_TOKEN=$(New-Secret)"
        $text = $text -replace "(?m)^BIZAGENT_WORKSPACE=[^\r\n]*", "BIZAGENT_WORKSPACE=$Workspace"
        [IO.File]::WriteAllText($EnvFile, $text, (New-Object Text.UTF8Encoding $false))
        Write-Host "    Mở .env điền API key của model AI (vd ANTHROPIC_API_KEY) và token kênh chat, rồi chạy lại script." -ForegroundColor Yellow
    }
} else {
    Step ".env đã có, giữ nguyên"
}

# 3. Kiểm tra cấu hình -----------------------------------------------------------------------
if (-not $DryRun) {
    Step "bizagent check -w $Workspace"
    & $Py -m bizagent check -w $Workspace
    if ($LASTEXITCODE) { Write-Host "    Còn lỗi cấu hình (xem trên). Service vẫn được cài; sửa xong thì Restart-Service $ServiceName." -ForegroundColor Yellow }
}

# 4. Quyền thư mục cho LocalService ----------------------------------------------------------
Step "Cấp quyền: đọc thư mục dự án, ghi workspaces\ và logs\ cho LocalService"
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
    icacls $Root /grant "${LocalService}:(OI)(CI)RX" /T /C /Q | Out-Null
    icacls (Join-Path $Root "workspaces") /grant "${LocalService}:(OI)(CI)M" /T /C /Q | Out-Null
    icacls (Join-Path $Root "logs") /grant "${LocalService}:(OI)(CI)M" /T /C /Q | Out-Null
}

# 5. File cấu hình WinSW ---------------------------------------------------------------------
$xml = Get-Content (Join-Path $PSScriptRoot "bizagent.xml.template") -Raw -Encoding UTF8
$xml = $xml.Replace("{{SERVICE}}", $ServiceName).Replace("{{WORKSPACE}}", $Workspace)
$xml = $xml.Replace("{{HOST}}", $ListenHost).Replace("{{PORT}}", "$Port")
if ($DryRun) {
    Step "File cấu hình service sẽ được ghi ra $SvcDir\$ServiceName.xml:"
    Write-Host $xml
    Step "DryRun: không cài service. Bỏ -DryRun để cài thật."
    exit 0
}
New-Item -ItemType Directory -Force -Path $SvcDir | Out-Null
[IO.File]::WriteAllText((Join-Path $SvcDir "$ServiceName.xml"), $xml, (New-Object Text.UTF8Encoding $false))
Copy-Item $WinSW (Join-Path $SvcDir "$ServiceName.exe") -Force

# 6. Cài và bật service ----------------------------------------------------------------------
$Exe = Join-Path $SvcDir "$ServiceName.exe"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Step "Service $ServiceName đã có: cập nhật cấu hình và khởi động lại"
    & $Exe stop | Out-Null
    & $Exe refresh
} else {
    Step "Cài service $ServiceName"
    & $Exe install
}
& $Exe start
Start-Sleep -Seconds 8
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 10
    Step "Đang chạy: bizagent $($h.version), workspace $Workspace. Mở http://127.0.0.1:$Port (chi tiết: bizagent check)"
} catch {
    Write-Host "Service chưa trả lời. Xem log: Get-Content $Root\logs\$ServiceName.err.log -Tail 50" -ForegroundColor Yellow
}
Write-Host "Theo dõi treo (khuyên dùng): powershell -ExecutionPolicy Bypass -File deploy\windows\healthcheck.ps1 -Register"
