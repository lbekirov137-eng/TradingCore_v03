#requires -Version 5.1
<#
TradingCore Stable PAPER Mode

Purpose: stop endless research from blocking the project and freeze one operational
PAPER champion: BTCUSDT 1H SESSION_VWAP_RANGE_LOW_VOL_PX.

Keeps:
- Main TradingCore PAPER 24x7
- BTC 1H Forward Shadow
- Collector B/C research in background (non-blocking)
- automatic first-seven BTC final gate

Removes only the stale Historical Accelerator V1 forward worker when its sealed
historical decision contains no candidate.

No terminal cleanup. No shutdown. No private API keys. No LIVE or real orders.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Stable = "C:\TradingCore_Stable_Paper"
$Gate = "$Repo\btc_1h_forward_final_gate.py"
$GateTask = "TradingCore BTC 1H Final Gate"
$GateLauncher = "$Stable\START_BTC_1H_FINAL_GATE.ps1"
$GateVbs = "$Stable\START_BTC_1H_FINAL_GATE_HIDDEN.vbs"
$ModePath = "$Stable\STABLE_PAPER_MODE.json"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "STABLE PAPER MODE STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing TradingCore services were not intentionally disabled." -ForegroundColor Green
    Write-Host "LIVE / real orders remain DISABLED." -ForegroundColor Green
    exit 1
}

function ReadJson([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function StartTaskIfNeeded([string]$Name) {
    $T = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $T) { Fail "Required Scheduled Task missing: $Name" }
    if ($T.State -eq "Disabled") { Fail "Required Scheduled Task is disabled: $Name" }
    if ($T.State -ne "Running") {
        Start-ScheduledTask -TaskName $Name -ErrorAction Stop
        Start-Sleep -Seconds 2
    }
}

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE STABLE PAPER MODE" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "Freeze research churn. Operate one BTC 1H PAPER champion."
Write-Host "No terminal cleanup. No shutdown. No API keys. No real orders."

if (-not (Test-Path $Repo)) { Fail "Repo missing: $Repo" }
if (-not (Test-Path $Py)) { Fail "Python missing: $Py" }
if (-not (Test-Path $Gate)) { Fail "Final-gate script missing: $Gate" }
New-Item -ItemType Directory -Force -Path $Stable | Out-Null

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
@(
    "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
    "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
    "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

# Compile + real import smoke test before touching tasks.
Push-Location $Repo
try {
    & $Py -m py_compile .\btc_1h_forward_final_gate.py .\btc_1h_bybit_confirmatory.py .\btc_1h_forward_shadow.py
    if ($LASTEXITCODE -ne 0) { Fail "Python compile failed." }
    & $Py -c "import btc_1h_forward_final_gate; print('FINAL GATE IMPORT: PASS')"
    if ($LASTEXITCODE -ne 0) { Fail "Final-gate dependency smoke import failed." }
} finally { Pop-Location }

# Remove only the known-stale historical forward worker if its sealed V1 research
# produced no candidate. This reduces background noise without touching evidence.
$HistDecision = ReadJson "C:\TradingCore_Historical_Accelerator\HISTORICAL_DECISION_LOCK.json"
$HistCandidate = Test-Path "C:\TradingCore_Historical_Accelerator\CANDIDATE_FOR_FORWARD_PAPER.json"
$HistTaskName = "TradingCore Historical Accelerator Forward PAPER"
$StaleHistoricalStopped = $false
if ($HistDecision -and -not $HistCandidate) {
    $T = Get-ScheduledTask -TaskName $HistTaskName -ErrorAction SilentlyContinue
    if ($T) {
        Stop-ScheduledTask -TaskName $HistTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $HistTaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*historical_accelerator_forward_paper.py*"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $StaleHistoricalStopped = $true
}

# Required operational core.
StartTaskIfNeeded "TradingCore PAPER 24x7"
StartTaskIfNeeded "TradingCore BTC 1H Forward Shadow"
Start-Sleep -Seconds 8

$Paper = $null
try { $Paper = Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5 } catch {}
if (-not $Paper -or $Paper.running -ne $true -or $Paper.real_orders_enabled -ne $false) {
    Fail "Main PAPER health verification failed."
}

$Shadow = ReadJson "C:\TradingCore_BTC_1H_SHADOW\status.json"
if (-not $Shadow -or $Shadow.running -ne $true -or $Shadow.real_orders_enabled -ne $false) {
    Fail "BTC 1H Forward Shadow health verification failed."
}

# Run final gate once now. Before 7 forward trades it only writes WAITING status.
Push-Location $Repo
try {
    & $Py .\btc_1h_forward_final_gate.py
    if ($LASTEXITCODE -ne 0) { Fail "BTC final gate returned non-zero." }
} finally { Pop-Location }

# Hidden self-healing gate controller. It does not place trades; every 5 minutes
# it checks whether the FIRST seven forward closes have become available.
@'
$ErrorActionPreference = "Continue"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Log = "C:\TradingCore_Stable_Paper\final_gate_supervisor.log"
$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
@(
  "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
  "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
  "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
Set-Location $Repo
while ($true) {
  "$(Get-Date -Format o) final gate check" | Add-Content $Log
  & $Py .\btc_1h_forward_final_gate.py >> $Log 2>&1
  Start-Sleep -Seconds 300
}
'@ | Set-Content $GateLauncher -Encoding UTF8

$Tokens=$null;$Errors=$null
[void][System.Management.Automation.Language.Parser]::ParseFile($GateLauncher,[ref]$Tokens,[ref]$Errors)
if ($Errors.Count -gt 0) { Fail "Generated final-gate launcher syntax failed." }

$PsExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$Command = '"' + $PsExe + '" -NoProfile -ExecutionPolicy Bypass -File "' + $GateLauncher + '"'
$Escaped = $Command.Replace('"','""')
@(
    'Option Explicit',
    'Dim sh, rc',
    'Set sh = CreateObject("WScript.Shell")',
    ('rc = sh.Run("{0}", 0, True)' -f $Escaped),
    'Set sh = Nothing'
) | Set-Content $GateVbs -Encoding ASCII

$Existing = Get-ScheduledTask -TaskName $GateTask -ErrorAction SilentlyContinue
if ($Existing) {
    Stop-ScheduledTask -TaskName $GateTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $GateTask -Confirm:$false -ErrorAction SilentlyContinue
}
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $GateVbs)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $GateTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $GateTask -ErrorAction Stop
Start-Sleep -Seconds 4
$GateScheduled = Get-ScheduledTask -TaskName $GateTask -ErrorAction Stop
if ($GateScheduled.State -ne "Running") { Fail "BTC final-gate hidden supervisor did not remain running." }

$GateStatus = ReadJson "$Stable\BTC_1H_FINAL_GATE_STATUS.json"
$Confirm = ReadJson "C:\TradingCore_BTC_1H_CONFIRMATORY\LATEST_BTC_1H_BYBIT_CONFIRMATORY.json"

$Mode = [ordered]@{
    schema = "TRADINGCORE_STABLE_PAPER_MODE_V1"
    entered_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    state = "STABLE_PAPER_OPERATIONAL"
    paper_champion = "SESSION_VWAP_RANGE_LOW_VOL_PX_1H"
    symbol = "BTCUSDT"
    execution_timeframe = "1h"
    status = "PROMISING_NOT_LIVE_VALIDATED"
    main_paper_healthy = $true
    btc_forward_shadow_healthy = $true
    btc_final_gate_task_running = $true
    historical_holdout_reference_trades = 23
    current_forward_closed_trades = if ($Shadow) { $Shadow.forward_closed_trades } else { $null }
    first_forward_decision_target = 7
    cross_venue_confirmatory_state = if ($Confirm) { $Confirm.state } else { $null }
    cross_venue_confirmatory_pf = if ($Confirm) { $Confirm.validation.oos_profit_factor } else { $null }
    cross_venue_confirmatory_expectancy_r = if ($Confirm) { $Confirm.validation.oos_expectancy_r } else { $null }
    cross_venue_confirmatory_trades = if ($Confirm) { $Confirm.validation.oos_trades } else { $null }
    final_gate_state = if ($GateStatus) { $GateStatus.state } else { $null }
    stale_historical_forward_worker_removed = $StaleHistoricalStopped
    forced_flow_collectors = "BACKGROUND_RESEARCH_NON_BLOCKING"
    research_factories = "FROZEN_AFTER_V3_NO_CANDIDATE"
    private_api_used = $false
    real_orders_enabled = $false
    live_trading_enabled = $false
}
$Mode | ConvertTo-Json -Depth 20 | Set-Content $ModePath -Encoding UTF8

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host " TRADINGCORE STABLE PAPER MODE: PASS" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "Operational champion: BTCUSDT 1H SESSION_VWAP_RANGE_LOW_VOL_PX"
Write-Host "Main PAPER: HEALTHY / 24x7"
Write-Host "BTC Forward Shadow: HEALTHY / HIDDEN"
Write-Host "Historical holdout reference: 23 trades"
Write-Host "Forward closed now: $($Shadow.forward_closed_trades) / first-decision target 7"
if ($Confirm) {
    Write-Host "Independent Bybit confirmation: trades=$($Confirm.validation.oos_trades) PF=$($Confirm.validation.oos_profit_factor) expR=$($Confirm.validation.oos_expectancy_r)"
}
if ($GateStatus) { Write-Host "Automatic final gate: $($GateStatus.state)" }
Write-Host "Research V1/V2/V3: DOES NOT BLOCK OPERATIONS"
Write-Host "Collector B/C: BACKGROUND ONLY"
Write-Host "Stale historical forward worker removed: $StaleHistoricalStopped"
Write-Host "Terminal windows: NOT TOUCHED"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Mode: $ModePath"
Write-Host "================================================================================" -ForegroundColor Green
