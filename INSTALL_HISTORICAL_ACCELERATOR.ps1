#requires -Version 5.1
<#
TradingCore Historical Accelerator installer/runner.

Runs the historical research immediately in the current terminal so progress and
final verdict are visible. Installs only the subsequent forward-PAPER worker as
a hidden WScript-owned Scheduled Task.

Does not stop V1/V2 collectors, does not close terminals, and cannot enable LIVE.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Historical_Accelerator"
$Logs = "$Root\logs"
$Control = "$Root\control"
$Launcher = "$Control\START_HISTORICAL_FORWARD_PAPER.ps1"
$Vbs = "$Control\START_HISTORICAL_FORWARD_PAPER_HIDDEN.vbs"
$TaskName = "TradingCore Historical Accelerator Forward PAPER"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "HISTORICAL ACCELERATOR STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing TradingCore V1/V2 services were not intentionally stopped." -ForegroundColor Green
    Write-Host "LIVE / real orders remain DISABLED." -ForegroundColor Green
    exit 1
}

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE HISTORICAL ACCELERATOR" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "730-day public historical research first; live collectors become confirmation only."
Write-Host "No terminal cleanup. No shutdown action. No API keys. No real orders."

if (-not (Test-Path $Repo)) { Fail "Repo missing: $Repo" }
if (-not (Test-Path $Py)) { Fail "Python missing: $Py" }

$Required = @(
    "historical_accelerator_protocol.py",
    "historical_accelerator.py",
    "historical_accelerator_selftest.py",
    "historical_accelerator_forward_paper.py"
)
foreach ($Name in $Required) {
    if (-not (Test-Path (Join-Path $Repo $Name))) { Fail "Required file missing: $Name" }
}

New-Item -ItemType Directory -Force -Path $Root,$Logs,$Control | Out-Null

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
@(
    "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
    "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
    "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

Push-Location $Repo
try {
    Write-Host ""
    Write-Host "Python compile check..." -ForegroundColor Cyan
    & $Py -m py_compile `
        .\historical_accelerator_protocol.py `
        .\historical_accelerator.py `
        .\historical_accelerator_selftest.py `
        .\historical_accelerator_forward_paper.py
    if ($LASTEXITCODE -ne 0) { Fail "Python compile check failed." }
    Write-Host "Compile: PASS" -ForegroundColor Green

    & $Py .\historical_accelerator_selftest.py
    if ($LASTEXITCODE -ne 0) { Fail "Historical Accelerator self-test failed." }

    Write-Host ""
    Write-Host "Starting historical download/backtest. First run can take several minutes; cached reruns are fast." -ForegroundColor Cyan
    & $Py .\historical_accelerator.py --state-dir $Root
    if ($LASTEXITCODE -ne 0) { Fail "Historical Accelerator returned non-zero." }
} finally {
    Pop-Location
}

$DecisionPath = "$Root\HISTORICAL_DECISION_LOCK.json"
if (-not (Test-Path $DecisionPath)) { Fail "Historical decision lock was not created." }
$Decision = Get-Content $DecisionPath -Raw | ConvertFrom-Json

# Hidden, self-healing forward PAPER launcher. It remains inert if there is no
# historical candidate marker.
@'
$ErrorActionPreference = "Continue"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Historical_Accelerator"
$Logs = "$Root\logs"
$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
@(
  "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
  "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
  "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Repo
while ($true) {
  "$(Get-Date -Format o) historical forward PAPER start" | Add-Content "$Logs\forward_supervisor.log"
  & $Py .\historical_accelerator_forward_paper.py --state-dir $Root --poll-seconds 30 >> "$Logs\forward.log" 2>&1
  "$(Get-Date -Format o) historical forward exited code=$LASTEXITCODE; restarting" | Add-Content "$Logs\forward_supervisor.log"
  Start-Sleep -Seconds 30
}
'@ | Set-Content $Launcher -Encoding UTF8

$Tokens=$null;$Errors=$null
[void][System.Management.Automation.Language.Parser]::ParseFile($Launcher,[ref]$Tokens,[ref]$Errors)
if ($Errors.Count -gt 0) { Fail "Generated forward launcher syntax failed." }

$PsExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$Command = '"' + $PsExe + '" -NoProfile -ExecutionPolicy Bypass -File "' + $Launcher + '"'
$Escaped = $Command.Replace('"','""')
@(
    'Option Explicit',
    'Dim sh, rc',
    'Set sh = CreateObject("WScript.Shell")',
    ('rc = sh.Run("{0}", 0, True)' -f $Escaped),
    'Set sh = Nothing'
) | Set-Content $Vbs -Encoding ASCII

try {
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $Vbs)
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch { Fail "Could not install hidden forward PAPER task: $($_.Exception.Message)" }

Start-Sleep -Seconds 8
$ForwardProc = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*historical_accelerator_forward_paper.py*"
})
if ($ForwardProc.Count -lt 1) { Fail "Forward PAPER worker did not remain running." }

$ForwardStatusPath = "$Root\forward_paper_status.json"
$ForwardStatus = $null
if (Test-Path $ForwardStatusPath) {
    try { $ForwardStatus = Get-Content $ForwardStatusPath -Raw | ConvertFrom-Json } catch {}
}

$InstallStatus = [ordered]@{
    schema = "TRADINGCORE_HISTORICAL_ACCELERATOR_INSTALL_V1"
    installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    historical_state = $Decision.state
    candidate_family = $Decision.candidate_family
    forward_worker_processes = $ForwardProc.Count
    forward_state = if ($ForwardStatus) { $ForwardStatus.state } else { $null }
    hidden_forward_task = $true
    real_orders_enabled = $false
    live_trading_enabled = $false
    collector_a_modified = $false
    collector_b_modified = $false
    collector_c_modified = $false
}
$InstallStatus | ConvertTo-Json -Depth 10 | Set-Content "$Root\INSTALL_STATUS.json" -Encoding UTF8

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host " HISTORICAL ACCELERATOR COMPLETE" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "Historical verdict: $($Decision.state)"
Write-Host "Candidate: $($Decision.candidate_family)"
Write-Host "Forward PAPER worker: RUNNING / HIDDEN"
if ($ForwardStatus) { Write-Host "Forward state: $($ForwardStatus.state)" }
Write-Host "V1 / Wide V2 collectors: LEFT RUNNING / confirmation only"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Report: $Root\LATEST_HISTORICAL_ACCELERATOR.json"
Write-Host "Decision: $DecisionPath"
Write-Host "================================================================================" -ForegroundColor Green
