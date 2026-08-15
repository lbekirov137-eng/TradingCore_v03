#requires -Version 5.1
<#
Install/repair parallel Wide V2 liquidation research stack.

Key properties:
- V1 PAPER / BTC shadow / Collector B are never stopped or modified.
- All Wide V2 scheduled tasks launch via wscript.exe, window style 0.
- Collector C starts FIRST and must create/restore UNIVERSE_LOCK + CONNECTED status.
- Only after Collector C is verified do the research orchestrator and forward PAPER worker start.
- Forward PAPER stays inert until historical PASS marker exists.
- PAPER/RESEARCH only. No private exchange credentials, no LIVE/order path.
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
$HiddenRoot = "C:\TradingCore_HiddenLaunchers"

$CollectorTask = "TradingCore Collector C Wide"
$OrchestratorTask = "TradingCore Wide Forced Flow Orchestrator"
$ForwardTask = "TradingCore Wide Forced Flow Forward PAPER"

$CollectorLauncher = "$Control\START_COLLECTOR_C_WIDE.ps1"
$OrchestratorLauncher = "$Control\START_WIDE_ORCHESTRATOR.ps1"
$ForwardLauncher = "$Control\START_WIDE_FORWARD_PAPER.ps1"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "WIDE V2 NOT INSTALLED / REPAIRED" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing V1 PAPER / Shadow / Collector B were NOT stopped." -ForegroundColor Green
    exit 1
}

function VbsEscape([string]$Text) {
    if ($null -eq $Text) { return "" }
    return $Text.Replace('"','""')
}

function New-HiddenVbs([string]$Name,[string]$PowerShellFile,[string]$WorkingDirectory) {
    New-Item -ItemType Directory -Force -Path $HiddenRoot | Out-Null
    $Vbs = Join-Path $HiddenRoot ($Name -replace '[^A-Za-z0-9._-]','_' )
    $Vbs += ".vbs"
    $Ps = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $Cmd = '"' + $Ps + '" -NoProfile -ExecutionPolicy Bypass -File "' + $PowerShellFile + '"'
    @(
        'Option Explicit',
        'Dim sh, rc',
        'Set sh = CreateObject("WScript.Shell")',
        ('sh.CurrentDirectory = "{0}"' -f (VbsEscape $WorkingDirectory)),
        ('rc = sh.Run("{0}", 0, True)' -f (VbsEscape $Cmd)),
        'Set sh = Nothing'
    ) | Set-Content $Vbs -Encoding ASCII
    return $Vbs
}

function Register-HiddenLongTask([string]$Name,[string]$Vbs) {
    $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Existing) {
        try { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Milliseconds 500
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
    }
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $Vbs)
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
}

function ProcCount([string]$Needle) {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*$Needle*"
    }).Count
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

New-Item -ItemType Directory -Force -Path $CollectorRoot,$Data,$Control,$Logs,$AutoRoot,$AutoLogs,$HiddenRoot | Out-Null

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
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
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
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
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
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET") | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
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

$CollectorVbs = New-HiddenVbs "TradingCore_Collector_C_Wide" $CollectorLauncher $Repo
$OrchestratorVbs = New-HiddenVbs "TradingCore_Wide_Forced_Flow_Orchestrator" $OrchestratorLauncher $Repo
$ForwardVbs = New-HiddenVbs "TradingCore_Wide_Forced_Flow_Forward_PAPER" $ForwardLauncher $Repo

# Phase 1: Collector C FIRST. This removes the historical race with UNIVERSE_LOCK.
Register-HiddenLongTask $CollectorTask $CollectorVbs
Start-ScheduledTask -TaskName $CollectorTask -ErrorAction Stop

$Universe="$Data\UNIVERSE_LOCK.json";$Status="$Data\status.json"
$CollectorReady=$false
for($i=0;$i -lt 40;$i++){
    Start-Sleep -Seconds 2
    if((Test-Path $Universe) -and (Test-Path $Status)){
        try {
            $CStatus=Get-Content $Status -Raw | ConvertFrom-Json
            if($CStatus.running -eq $true -and $CStatus.connection_state -eq "CONNECTED" -and $CStatus.real_orders_enabled -eq $false){$CollectorReady=$true;break}
        } catch {}
    }
}
if(-not $CollectorReady){ Fail "Collector C did not reach CONNECTED safely. Check $Logs\collector.log" }

# Initial read-only audit. Zero events is normal immediately after startup.
Push-Location $Repo
try { & $Py .\collector_c_g2_g3_audit.py --data-dir $Data } finally { Pop-Location }
$Audit=Get-Content "$Repo\collector_c_audit_results\LATEST_COLLECTOR_C_G2_G3.json" -Raw | ConvertFrom-Json
if($Audit.g2.state -eq "G2_REPAIR_REQUIRED" -or $Audit.g3.state -eq "G3_REPAIR_REQUIRED"){ Fail "Collector C initial data-quality audit requires repair." }

# Phase 2: only now start orchestrator and Forward PAPER.
Register-HiddenLongTask $OrchestratorTask $OrchestratorVbs
Register-HiddenLongTask $ForwardTask $ForwardVbs
Start-ScheduledTask -TaskName $OrchestratorTask -ErrorAction Stop
Start-ScheduledTask -TaskName $ForwardTask -ErrorAction Stop

Start-Sleep -Seconds 15
$CollectorProc=ProcCount "collector_c_bybit_wide.py"
$OrchProc=ProcCount "forced_flow_wide_autonomous_orchestrator.py"
$ForwardProc=ProcCount "forced_flow_wide_forward_paper.py"
$CTask=Get-ScheduledTask -TaskName $CollectorTask -ErrorAction SilentlyContinue
$OTask=Get-ScheduledTask -TaskName $OrchestratorTask -ErrorAction SilentlyContinue
$FTask=Get-ScheduledTask -TaskName $ForwardTask -ErrorAction SilentlyContinue

if($CollectorProc -lt 1 -or $OrchProc -lt 1 -or $ForwardProc -lt 1){
    Fail "Wide V2 process verification failed: collector=$CollectorProc orchestrator=$OrchProc forward=$ForwardProc"
}
if($CTask.State -ne "Running" -or $OTask.State -ne "Running" -or $FTask.State -ne "Running"){
    Fail "Wide V2 hidden Scheduled Tasks did not remain Running."
}

$Lock=Get-Content $Universe -Raw | ConvertFrom-Json
@{
 schema="TRADINGCORE_WIDE_V2_INSTALL_STATUS_V2"
 installed_at_utc=(Get-Date).ToUniversalTime().ToString("o")
 universe_symbols=$Lock.symbols
 universe_fingerprint=$Lock.fingerprint
 collector_state="CONNECTED"
 g2=$Audit.g2.state
 g3=$Audit.g3.state
 protocol="FORCED_FLOW_WIDE_REBOUND_V2"
 task_backend="WSCRIPT_HIDDEN_WAIT"
 collector_processes=$CollectorProc
 orchestrator_processes=$OrchProc
 forward_paper_processes=$ForwardProc
 real_orders_enabled=$false
 live_trading_enabled=$false
 collector_a_modified=$false
 collector_b_modified=$false
} | ConvertTo-Json -Depth 20 | Set-Content "$AutoRoot\INSTALL_STATUS.json" -Encoding UTF8

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE WIDE V2 INSTALLED / REPAIRED" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Wide Collector C: CONNECTED / HIDDEN / self-healing"
Write-Host "Universe: $($Lock.symbols.Count) frozen symbols"
Write-Host "Universe fingerprint: $($Lock.fingerprint)"
Write-Host "Wide G2: $($Audit.g2.state)"
Write-Host "Wide G3: $($Audit.g3.state)"
Write-Host "Wide Orchestrator: RUNNING / HIDDEN"
Write-Host "Wide Forward PAPER: RUNNING / HIDDEN / inert until historical PASS"
Write-Host "V1 BTC/ETH/SOL: UNCHANGED" -ForegroundColor Green
Write-Host "Collector A/B: UNCHANGED" -ForegroundColor Green
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
