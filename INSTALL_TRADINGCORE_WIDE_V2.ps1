#requires -Version 5.1
<#
Install parallel Wide V2 liquidation research stack.
Does NOT stop or modify V1 TradingCore/Collector B tasks.
Fail-closed: selftest must pass before any new Scheduled Task is created.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$CollectorRoot = "C:\TradingCore_Collector_C"
$Data = "$CollectorRoot\data"
$Control = "$CollectorRoot\control"
$Logs = "$CollectorRoot\logs"
$AutoRoot = "C:\TradingCore_Wide_Autonomous"
$AutoLogs = "$AutoRoot\logs"

$CollectorTask = "TradingCore Collector C Wide"
$OrchestratorTask = "TradingCore Wide Forced Flow Orchestrator"
$ForwardTask = "TradingCore Wide Forced Flow Forward PAPER"

$CollectorLauncher = "$Control\START_COLLECTOR_C_WIDE.ps1"
$OrchestratorLauncher = "$Control\START_WIDE_ORCHESTRATOR.ps1"
$ForwardLauncher = "$Control\START_WIDE_FORWARD_PAPER.ps1"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "WIDE V2 NOT INSTALLED" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing V1 PAPER / Shadow / Collector B were NOT stopped." -ForegroundColor Green
    exit 1
}

if (-not (Test-Path $Repo)) { Fail "TradingCore recovery repo missing: $Repo" }
if (-not (Test-Path $Py)) { Fail "Collector B isolated Python missing: $Py" }

$Required = @(
    "collector_c_bybit_wide.py",
    "collector_c_g2_g3_audit.py",
    "forced_flow_wide_protocol.py",
    "forced_flow_wide_research_engine.py",
    "forced_flow_wide_research_portfolio_safe.py",
    "forced_flow_wide_autonomous_orchestrator.py",
    "forced_flow_wide_forward_paper.py",
    "forced_flow_wide_selftest.py"
)
foreach ($Name in $Required) {
    if (-not (Test-Path (Join-Path $Repo $Name))) { Fail "Required file missing: $Name" }
}

New-Item -ItemType Directory -Force -Path $CollectorRoot,$Data,$Control,$Logs,$AutoRoot,$AutoLogs | Out-Null

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
$env:COLLECTOR_C_DATA_DIR = $Data

Push-Location $Repo
try {
    & $Py .\forced_flow_wide_selftest.py
    $SelfCode = $LASTEXITCODE
} finally { Pop-Location }
if ($SelfCode -ne 0) { Fail "Wide V2 safety self-test failed." }

@'
$ErrorActionPreference = "Continue"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root="C:\TradingCore_Collector_C"
$Data="$Root\data"
$Logs="$Root\logs"
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true";$env:COLLECTOR_C_DATA_DIR=$Data
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | % { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Logs,$Data | Out-Null
Set-Location $Repo
while($true){
  "$(Get-Date -Format o) Collector C start" | Add-Content "$Logs\supervisor.log"
  & $Py .\collector_c_bybit_wide.py >> "$Logs\collector.log" 2>&1
  "$(Get-Date -Format o) Collector C exited code=$LASTEXITCODE; restart" | Add-Content "$Logs\supervisor.log"
  Start-Sleep -Seconds 30
}
'@ | Set-Content $CollectorLauncher -Encoding UTF8

@'
$ErrorActionPreference = "Continue"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Data="C:\TradingCore_Collector_C\data"
$Root="C:\TradingCore_Wide_Autonomous"
$Logs="$Root\logs"
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true";$env:COLLECTOR_C_DATA_DIR=$Data
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | % { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Repo
while($true){
  "$(Get-Date -Format o) Wide orchestrator start" | Add-Content "$Logs\orchestrator_supervisor.log"
  & $Py .\forced_flow_wide_autonomous_orchestrator.py --data-dir $Data --state-dir $Root --python $Py --interval-seconds 900 >> "$Logs\orchestrator.log" 2>&1
  "$(Get-Date -Format o) Wide orchestrator exited code=$LASTEXITCODE; restart" | Add-Content "$Logs\orchestrator_supervisor.log"
  Start-Sleep -Seconds 30
}
'@ | Set-Content $OrchestratorLauncher -Encoding UTF8

@'
$ErrorActionPreference = "Continue"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Data="C:\TradingCore_Collector_C\data"
$Root="C:\TradingCore_Wide_Autonomous"
$Logs="$Root\logs"
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true";$env:COLLECTOR_C_DATA_DIR=$Data
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | % { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Repo
while($true){
  "$(Get-Date -Format o) Wide forward PAPER start" | Add-Content "$Logs\forward_supervisor.log"
  & $Py .\forced_flow_wide_forward_paper.py --data-dir $Data --state-dir $Root --poll-seconds 2 >> "$Logs\forward.log" 2>&1
  "$(Get-Date -Format o) Wide forward exited code=$LASTEXITCODE; restart" | Add-Content "$Logs\forward_supervisor.log"
  Start-Sleep -Seconds 30
}
'@ | Set-Content $ForwardLauncher -Encoding UTF8

foreach ($Script in @($CollectorLauncher,$OrchestratorLauncher,$ForwardLauncher)) {
    $Tokens=$null;$Errors=$null
    [void][System.Management.Automation.Language.Parser]::ParseFile($Script,[ref]$Tokens,[ref]$Errors)
    if ($Errors.Count -gt 0) { Fail "Generated launcher syntax failed: $Script" }
}

foreach ($Name in @($CollectorTask,$OrchestratorTask,$ForwardTask)) {
    $Existing=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Existing) {
        try { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Milliseconds 500
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
    }
}

try {
    $User=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Trigger=New-ScheduledTaskTrigger -AtLogOn -User $User
    $Principal=New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    $Specs=@(
        @{Name=$CollectorTask;File=$CollectorLauncher},
        @{Name=$OrchestratorTask;File=$OrchestratorLauncher},
        @{Name=$ForwardTask;File=$ForwardLauncher}
    )
    foreach($Spec in $Specs){
        $Action=New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($Spec.File)`""
        Register-ScheduledTask -TaskName $Spec.Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
        Start-ScheduledTask -TaskName $Spec.Name -ErrorAction Stop
    }
} catch { Fail "Could not create/start Wide V2 tasks: $($_.Exception.Message)" }

# Give Collector C time to create its frozen public universe and connect.
$Universe="$Data\UNIVERSE_LOCK.json";$Status="$Data\status.json"
for($i=0;$i -lt 30;$i++){
    if((Test-Path $Universe) -and (Test-Path $Status)){break}
    Start-Sleep -Seconds 2
}
if(-not (Test-Path $Universe)){ Fail "Collector C did not create UNIVERSE_LOCK. Check $Logs\collector.log" }
if(-not (Test-Path $Status)){ Fail "Collector C did not create status.json. Check $Logs\collector.log" }

Start-Sleep -Seconds 5
$CStatus=Get-Content $Status -Raw | ConvertFrom-Json
if($CStatus.running -ne $true -or $CStatus.connection_state -ne "CONNECTED" -or $CStatus.real_orders_enabled -ne $false){ Fail "Collector C runtime safety/connection verification failed." }

# Fresh audit; zero events immediately after start is allowed as G2 pending sample.
Push-Location $Repo
try { & $Py .\collector_c_g2_g3_audit.py --data-dir $Data } finally { Pop-Location }
$Audit=Get-Content "$Repo\collector_c_audit_results\LATEST_COLLECTOR_C_G2_G3.json" -Raw | ConvertFrom-Json
if($Audit.g2.state -eq "G2_REPAIR_REQUIRED" -or $Audit.g3.state -eq "G3_REPAIR_REQUIRED"){ Fail "Collector C initial data-quality audit requires repair." }

Start-Sleep -Seconds 5
$CollectorProc=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ? { $_.CommandLine -like "*collector_c_bybit_wide.py*" })
$OrchProc=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ? { $_.CommandLine -like "*forced_flow_wide_autonomous_orchestrator.py*" })
$ForwardProc=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ? { $_.CommandLine -like "*forced_flow_wide_forward_paper.py*" })
if($CollectorProc.Count -lt 1 -or $OrchProc.Count -lt 1 -or $ForwardProc.Count -lt 1){ Fail "One or more Wide V2 workers failed to remain running." }

$Lock=Get-Content $Universe -Raw | ConvertFrom-Json
@{
 schema="TRADINGCORE_WIDE_V2_INSTALL_STATUS"
 installed_at_utc=(Get-Date).ToUniversalTime().ToString("o")
 universe_symbols=$Lock.symbols
 universe_fingerprint=$Lock.fingerprint
 collector_state=$CStatus.connection_state
 g2=$Audit.g2.state
 g3=$Audit.g3.state
 protocol="FORCED_FLOW_WIDE_REBOUND_V2"
 real_orders_enabled=$false
 live_trading_enabled=$false
 collector_a_modified=$false
 collector_b_modified=$false
} | ConvertTo-Json -Depth 20 | Set-Content "$AutoRoot\INSTALL_STATUS.json" -Encoding UTF8

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE WIDE V2 INSTALLED" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Wide Collector C: CONNECTED / self-healing"
Write-Host "Universe: $($Lock.symbols.Count) frozen symbols"
Write-Host "Universe fingerprint: $($Lock.fingerprint)"
Write-Host "Wide G2: $($Audit.g2.state)"
Write-Host "Wide G3: $($Audit.g3.state)"
Write-Host "Research: AUTOMATIC after preregistered epoch gate"
Write-Host "Forward PAPER: worker running / inert until historical PASS"
Write-Host "V1 BTC/ETH/SOL: UNCHANGED" -ForegroundColor Green
Write-Host "Collector A/B: UNCHANGED" -ForegroundColor Green
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
