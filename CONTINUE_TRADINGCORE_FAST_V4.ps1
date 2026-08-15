#requires -Version 5.1
<#
TradingCore FAST TRACK V4.1

Goal: continue the project quickly without terminal cleanup or manual multi-step work.
- Does NOT close Windows Terminal or the user's interactive shells.
- Does NOT request/cancel Windows shutdown.
- Keeps V1 PAPER / Shadow / Collector B alive.
- Repairs/starts Wide V2 using the corrected sequential hidden installer.
- Runs fresh read-only G2/G3 audits for narrow V1 and Wide V2.
- Verifies live worker processes and writes one unified status file.
- PAPER/RESEARCH only; no private exchange API/order/LIVE path.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$WideInstaller = "$Repo\INSTALL_TRADINGCORE_WIDE_V2.ps1"
$OutRoot = "C:\TradingCore_Autonomous"
$Out = "$OutRoot\FAST_TRACK_V4_STATUS.json"

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}
function ProcCount([string]$Needle) {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*$Needle*"
    }).Count
}
function Start-IfPresent([string]$Name) {
    $T = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($T -and $T.State -ne "Running" -and $T.State -ne "Disabled") {
        Start-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}
function Is-HiddenTask([string]$Name) {
    $T = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $T) { return $false }
    $A = @($T.Actions)
    if ($A.Count -ne 1) { return $false }
    return ([string]$A[0].Execute -match '(?i)wscript\.exe$')
}
function Fail([string]$Reason) {
    New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
    @{
        schema="TRADINGCORE_FAST_TRACK_V4_1"
        state="FAILED_SAFE"
        reason=$Reason
        updated_at_utc=(Get-Date).ToUniversalTime().ToString("o")
        windows_terminal_cleanup=$false
        real_orders_enabled=$false
        live_trading_enabled=$false
        collector_a_modified=$false
    } | ConvertTo-Json -Depth 10 | Set-Content $Out -Encoding UTF8
    Write-Host ""
    Write-Host "FAST TRACK V4.1 STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Reason -ForegroundColor Yellow
    Write-Host "Existing services were not intentionally stopped. No terminal cleanup was performed." -ForegroundColor Green
    exit 1
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE FAST TRACK V4.1" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "No Windows Terminal cleanup. No shutdown action. V1 stays alive. Wide V2 will be repaired/started."

if (-not (Test-Path $Repo)) { Fail "TradingCore repo missing: $Repo" }
if (-not (Test-Path $Py)) { Fail "Collector B Python missing: $Py" }
if (-not (Test-Path $WideInstaller)) { Fail "Corrected Wide V2 installer missing: $WideInstaller" }

# Ensure V1 long-running stack is alive before touching Wide V2.
foreach ($Name in @(
    "TradingCore PAPER 24x7",
    "TradingCore BTC 1H Forward Shadow",
    "TradingCore Collector B",
    "TradingCore Forced Flow Autonomous Orchestrator",
    "TradingCore Forced Flow Forward PAPER"
)) { Start-IfPresent $Name }

Start-Sleep -Seconds 5

# Syntax-check corrected Wide V2 installer before executing it.
$Tokens=$null;$Errors=$null
[void][System.Management.Automation.Language.Parser]::ParseFile($WideInstaller,[ref]$Tokens,[ref]$Errors)
if ($Errors.Count -gt 0) {
    Fail ("Wide installer PowerShell syntax failed: " + (($Errors | ForEach-Object {$_.Message}) -join " | "))
}

# Repair/start Wide V2. This installer touches only Wide tasks/data.
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $WideInstaller
$WideExit = $LASTEXITCODE
$ErrorActionPreference = $OldPreference
if ($WideExit -ne 0) { Fail "Wide V2 corrected installer returned non-zero." }

# Refresh periodic V1 integrity jobs once. Their scheduled cadence remains intact.
foreach ($Name in @(
    "TradingCore Collector B G2 G3 Auto Audit",
    "TradingCore Collector B G3 Gap Guardian"
)) { Start-IfPresent $Name }

# Explicit fresh read-only audits so the final status is current.
$env:TRADING_ENVIRONMENT="PAPER"
$env:LIVE_TRADING="false"
$env:PAPER_TRADING="true"
$env:DEMO_ONLY="true"

Push-Location $Repo
try {
    $env:COLLECTOR_B_DATA_DIR="C:\TradingCore_Collector_B\data"
    & $Py .\collector_b_g2_g3_audit.py --data-dir "C:\TradingCore_Collector_B\data" *> "$OutRoot\FAST_V4_B_AUDIT.log"
    $env:COLLECTOR_C_DATA_DIR="C:\TradingCore_Collector_C\data"
    & $Py .\collector_c_g2_g3_audit.py --data-dir "C:\TradingCore_Collector_C\data" *> "$OutRoot\FAST_V4_C_AUDIT.log"
} finally { Pop-Location }

Start-Sleep -Seconds 8

$Paper=$null
try { $Paper=Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5 } catch {}
$Shadow=Read-JsonSafe "C:\TradingCore_BTC_1H_SHADOW\status.json"
$B=Read-JsonSafe "C:\TradingCore_Collector_B\data\status.json"
$BAudit=Read-JsonSafe "$Repo\collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json"
$V1Auto=Read-JsonSafe "C:\TradingCore_Autonomous\status.json"
$V1Forward=Read-JsonSafe "C:\TradingCore_Autonomous\forward_paper_status.json"
$C=Read-JsonSafe "C:\TradingCore_Collector_C\data\status.json"
$CLock=Read-JsonSafe "C:\TradingCore_Collector_C\data\UNIVERSE_LOCK.json"
$CAudit=Read-JsonSafe "$Repo\collector_c_audit_results\LATEST_COLLECTOR_C_G2_G3.json"
$WideAuto=Read-JsonSafe "C:\TradingCore_Wide_Autonomous\status.json"
$WideForward=Read-JsonSafe "C:\TradingCore_Wide_Autonomous\forward_paper_status.json"

$Proc=[ordered]@{
    main_paper_api = [bool]($Paper -and $Paper.running -eq $true -and $Paper.real_orders_enabled -eq $false)
    btc_shadow = [bool]($Shadow -and $Shadow.running -eq $true -and $Shadow.real_orders_enabled -eq $false)
    collector_b = [bool]($B -and $B.running -eq $true -and $B.connection_state -eq "CONNECTED" -and $B.real_orders_enabled -eq $false)
    v1_orchestrator = ProcCount "forced_flow_autonomous_orchestrator.py"
    v1_forward = ProcCount "forced_flow_forward_paper.py"
    collector_c = ProcCount "collector_c_bybit_wide.py"
    wide_orchestrator = ProcCount "forced_flow_wide_autonomous_orchestrator.py"
    wide_forward = ProcCount "forced_flow_wide_forward_paper.py"
}

$Hidden=[ordered]@{}
foreach ($Name in @(
    "TradingCore PAPER 24x7",
    "TradingCore BTC 1H Forward Shadow",
    "TradingCore Collector B",
    "TradingCore Forced Flow Autonomous Orchestrator",
    "TradingCore Forced Flow Forward PAPER",
    "TradingCore Collector C Wide",
    "TradingCore Wide Forced Flow Orchestrator",
    "TradingCore Wide Forced Flow Forward PAPER"
)) { $Hidden[$Name]=Is-HiddenTask $Name }

$Critical=@()
if(-not $Proc.main_paper_api){$Critical += "MAIN_PAPER"}
if(-not $Proc.btc_shadow){$Critical += "BTC_SHADOW"}
if(-not $Proc.collector_b){$Critical += "COLLECTOR_B"}
if([int]$Proc.v1_orchestrator -lt 1){$Critical += "V1_ORCHESTRATOR"}
if([int]$Proc.v1_forward -lt 1){$Critical += "V1_FORWARD"}
if([int]$Proc.collector_c -lt 1){$Critical += "COLLECTOR_C"}
if([int]$Proc.wide_orchestrator -lt 1){$Critical += "WIDE_ORCHESTRATOR"}
if([int]$Proc.wide_forward -lt 1){$Critical += "WIDE_FORWARD"}
if(-not $C -or $C.connection_state -ne "CONNECTED" -or $C.real_orders_enabled -ne $false){$Critical += "COLLECTOR_C_HEALTH"}
if(-not $CLock -or @($CLock.symbols).Count -lt 10){$Critical += "WIDE_UNIVERSE_LOCK"}

$State=if($Critical.Count -eq 0){"RUNNING_AUTONOMOUS"}else{"FAILED_SAFE_ATTENTION"}
$Summary=[ordered]@{
    schema="TRADINGCORE_FAST_TRACK_V4_1"
    state=$State
    updated_at_local=(Get-Date).ToString("o")
    critical_failures=$Critical
    processes=$Proc
    hidden_long_running_tasks=$Hidden
    v1=[ordered]@{
        g2=if($BAudit){$BAudit.g2.state}else{$null}
        g3=if($BAudit){$BAudit.g3.state}else{$null}
        events=if($BAudit){$BAudit.evidence.valid_unique_events}else{$null}
        span_hours=if($BAudit){$BAudit.evidence.observation_span_hours}else{$null}
        autonomous_state=if($V1Auto){$V1Auto.state}else{$null}
        forward_state=if($V1Forward){$V1Forward.state}else{$null}
    }
    wide_v2=[ordered]@{
        universe_count=if($CLock){@($CLock.symbols).Count}else{0}
        universe_fingerprint=if($CLock){$CLock.fingerprint}else{$null}
        current_epoch=if($CAudit){$CAudit.current_epoch}else{$null}
        g2=if($CAudit){$CAudit.g2.state}else{$null}
        g3=if($CAudit){$CAudit.g3.state}else{$null}
        events=if($CAudit){$CAudit.evidence.valid_unique_events}else{$null}
        span_hours=if($CAudit){$CAudit.evidence.observation_span_hours}else{$null}
        autonomous_state=if($WideAuto){$WideAuto.state}else{$null}
        research_state=if($WideAuto){$WideAuto.research_state}else{$null}
        forward_state=if($WideForward){$WideForward.state}else{$null}
        forward_authorized=if($WideForward){$WideForward.authorized}else{$false}
    }
    windows_terminal_cleanup=$false
    private_api_used=$false
    real_orders_enabled=$false
    live_trading_enabled=$false
    collector_a_modified=$false
}
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
$Summary | ConvertTo-Json -Depth 20 | Set-Content $Out -Encoding UTF8

Write-Host ""
Write-Host "==================================================" -ForegroundColor $(if($Critical.Count -eq 0){"Green"}else{"Red"})
Write-Host " TRADINGCORE FAST TRACK V4.1: $State" -ForegroundColor $(if($Critical.Count -eq 0){"Green"}else{"Red"})
Write-Host "=================================================="
Write-Host "V1: G2=$($Summary.v1.g2) G3=$($Summary.v1.g3) events=$($Summary.v1.events)"
Write-Host "Wide V2: universe=$($Summary.wide_v2.universe_count) G2=$($Summary.wide_v2.g2) G3=$($Summary.wide_v2.g3) events=$($Summary.wide_v2.events)"
Write-Host "Wide research: $($Summary.wide_v2.autonomous_state)"
Write-Host "Wide forward PAPER: $($Summary.wide_v2.forward_state)"
Write-Host "Terminal windows: NOT TOUCHED"
Write-Host "Future Wide tasks: HIDDEN via WScript"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Status: $Out"
if($Critical.Count -gt 0){
    Write-Host "Attention: $($Critical -join ', ')" -ForegroundColor Yellow
    exit 1
}
