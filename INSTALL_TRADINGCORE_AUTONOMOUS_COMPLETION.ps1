#requires -Version 5.1
<#
Installs the autonomous forced-flow completion layer without stopping any
existing TradingCore tasks. Fail-closed: self-test must pass first.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Autonomous"
$Control = "$Root\control"
$Logs = "$Root\logs"
$Data = "C:\TradingCore_Collector_B\data"

$OrchestratorTask = "TradingCore Forced Flow Autonomous Orchestrator"
$ForwardTask = "TradingCore Forced Flow Forward PAPER"
$OrchestratorLauncher = "$Control\START_FORCED_FLOW_ORCHESTRATOR.ps1"
$ForwardLauncher = "$Control\START_FORCED_FLOW_FORWARD_PAPER.ps1"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "AUTONOMOUS COMPLETION NOT INSTALLED" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing TradingCore processes were NOT stopped." -ForegroundColor Green
    exit 1
}

if (-not (Test-Path $Py)) { Fail "Collector B isolated Python not found: $Py" }
if (-not (Test-Path $Data)) { Fail "Collector B data directory not found: $Data" }

$Required = @(
    "forced_flow_protocol.py",
    "forced_flow_research_engine.py",
    "forced_flow_autonomous_orchestrator.py",
    "forced_flow_forward_paper.py",
    "forced_flow_selftest.py"
)
foreach ($Name in $Required) {
    if (-not (Test-Path (Join-Path $Repo $Name))) {
        Fail "Required file missing: $Name"
    }
}

New-Item -ItemType Directory -Force -Path $Root,$Control,$Logs | Out-Null

# Safety self-test BEFORE creating or starting any new task.
$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
$env:COLLECTOR_B_DATA_DIR = $Data

Push-Location $Repo
try {
    & $Py .\forced_flow_selftest.py
    $SelfTestCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($SelfTestCode -ne 0) { Fail "Forced-flow safety self-test failed." }

@'
$ErrorActionPreference = "Continue"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Autonomous"
$Logs = "$Root\logs"
$Data = "C:\TradingCore_Collector_B\data"

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
$env:COLLECTOR_B_DATA_DIR = $Data

@(
    "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
    "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
    "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Repo

while ($true) {
    "$(Get-Date -Format o) forced-flow orchestrator start" | Add-Content "$Logs\orchestrator_supervisor.log"
    & $Py .\forced_flow_autonomous_orchestrator.py `
        --data-dir $Data `
        --state-dir $Root `
        --python $Py `
        --interval-seconds 900 `
        >> "$Logs\orchestrator.log" 2>&1
    $Code = $LASTEXITCODE
    "$(Get-Date -Format o) orchestrator exited code=$Code; restarting" | Add-Content "$Logs\orchestrator_supervisor.log"
    Start-Sleep -Seconds 30
}
'@ | Set-Content $OrchestratorLauncher -Encoding UTF8

@'
$ErrorActionPreference = "Continue"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Autonomous"
$Logs = "$Root\logs"
$Data = "C:\TradingCore_Collector_B\data"

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
$env:COLLECTOR_B_DATA_DIR = $Data

@(
    "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
    "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
    "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Repo

while ($true) {
    "$(Get-Date -Format o) forced-flow forward PAPER worker start" | Add-Content "$Logs\forward_paper_supervisor.log"
    & $Py .\forced_flow_forward_paper.py `
        --data-dir $Data `
        --state-dir $Root `
        --poll-seconds 5 `
        >> "$Logs\forward_paper.log" 2>&1
    $Code = $LASTEXITCODE
    "$(Get-Date -Format o) forward PAPER exited code=$Code; restarting" | Add-Content "$Logs\forward_paper_supervisor.log"
    Start-Sleep -Seconds 30
}
'@ | Set-Content $ForwardLauncher -Encoding UTF8

# Parse generated launchers before touching Task Scheduler.
foreach ($Script in @($OrchestratorLauncher,$ForwardLauncher)) {
    $Tokens = $null
    $Errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($Script,[ref]$Tokens,[ref]$Errors)
    if ($Errors.Count -gt 0) {
        Fail "Generated launcher syntax check failed: $Script"
    }
}

# Idempotent Scheduled Tasks setup. A missing old task is NORMAL and must not
# fail a first installation. Avoid schtasks.exe native-stderr behaviour under
# $ErrorActionPreference='Stop'.
foreach ($Name in @($OrchestratorTask,$ForwardTask)) {
    try {
        $ExistingTask = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($ExistingTask) {
            if ($ExistingTask.State -eq "Running") {
                Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
        }
    } catch {
        Fail "Could not replace scheduled task ${Name}: $($_.Exception.Message)"
    }
}

try {
    $CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

    $Action1 = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$OrchestratorLauncher`""
    )
    $Action2 = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ForwardLauncher`""
    )
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $OrchestratorTask -Action $Action1 -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
    Register-ScheduledTask -TaskName $ForwardTask -Action $Action2 -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
} catch {
    Fail "Could not create autonomous Scheduled Tasks: $($_.Exception.Message)"
}

try {
    Start-ScheduledTask -TaskName $OrchestratorTask -ErrorAction Stop
    Start-ScheduledTask -TaskName $ForwardTask -ErrorAction Stop
} catch {
    Fail "Could not start autonomous Scheduled Tasks: $($_.Exception.Message)"
}

Start-Sleep -Seconds 12

$OrchProc = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*forced_flow_autonomous_orchestrator.py*"
})
$PaperProc = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*forced_flow_forward_paper.py*"
})

if ($OrchProc.Count -lt 1 -or $PaperProc.Count -lt 1) {
    Fail "One or more autonomous worker processes failed to start. Check $Logs."
}

# Resolve protocol version from the repository directory so Python can import
# the local frozen protocol module regardless of the caller's current folder.
Push-Location $Repo
try {
    $ProtocolVersion = (& $Py -c "import forced_flow_protocol as p; print(p.PROTOCOL_VERSION)" | Select-Object -Last 1)
    $ProtocolCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($ProtocolCode -ne 0 -or [string]::IsNullOrWhiteSpace($ProtocolVersion)) {
    Fail "Could not read frozen forced-flow protocol version."
}

$Status = @{
    schema = "TRADINGCORE_AUTONOMOUS_COMPLETION_INSTALL_V3"
    installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    orchestrator_processes = $OrchProc.Count
    forward_paper_processes = $PaperProc.Count
    protocol = $ProtocolVersion
    task_scheduler_backend = "POWERSHELL_SCHEDULEDTASKS"
    real_orders_enabled = $false
    live_trading_enabled = $false
    collector_a_modified = $false
    note = "Forward PAPER worker is intentionally inert until historical research creates PAPER-only authorization."
}
$Status | ConvertTo-Json -Depth 10 | Set-Content "$Root\INSTALL_STATUS.json" -Encoding UTF8

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE AUTONOMOUS COMPLETION INSTALLED" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Research orchestrator: RUNNING / self-healing"
Write-Host "Forward PAPER worker: RUNNING / waiting for research PASS"
Write-Host "Protocol: $ProtocolVersion / FROZEN"
Write-Host "G2/G3 -> sample gate -> strict holdout -> PAPER: AUTOMATIC"
Write-Host "LIVE trading: BLOCKED" -ForegroundColor Green
Write-Host "Real orders: DISABLED" -ForegroundColor Green
Write-Host "Collector A: UNCHANGED" -ForegroundColor Green
Write-Host "State: $Root"
