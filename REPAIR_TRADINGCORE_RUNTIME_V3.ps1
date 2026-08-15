#requires -Version 5.1
<#
TradingCore Runtime Repair V3
- makes every TradingCore Scheduled Task truly silent using WScript window=0;
- keeps long-running tasks owned by Task Scheduler using WaitOnReturn=True;
- distinguishes long-running services from periodic one-shot audit jobs;
- restarts/validates V1 and Wide V2 runtime without touching strategy data;
- closes old Windows Terminal windows only after runtime verification passes.

PAPER/RESEARCH ONLY. No LIVE/order changes.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$HiddenRoot = "C:\TradingCore_HiddenLaunchers"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Backup = "$HiddenRoot\TASK_ACTION_BACKUP_V3_$Stamp.json"
$StatusPath = "$HiddenRoot\LATEST_STATUS_V3.json"
$ContinuousSupervisor = "$Repo\RUN_5H_OVERNIGHT_SUPERVISOR.ps1"

$LongRunning = @(
    "TradingCore PAPER 24x7",
    "TradingCore BTC 1H Forward Shadow",
    "TradingCore Collector B",
    "TradingCore Forced Flow Autonomous Orchestrator",
    "TradingCore Forced Flow Forward PAPER",
    "TradingCore Collector C Wide",
    "TradingCore Wide Forced Flow Orchestrator",
    "TradingCore Wide Forced Flow Forward PAPER"
)
$Periodic = @(
    "TradingCore Collector B G2 G3 Auto Audit",
    "TradingCore Collector B G3 Gap Guardian"
)

New-Item -ItemType Directory -Force -Path $HiddenRoot | Out-Null

function VbsEscape([string]$Text) {
    if ($null -eq $Text) { return "" }
    return $Text.Replace('"','""')
}
function SafeName([string]$Text) {
    return ($Text -replace '[^A-Za-z0-9._-]', '_').Trim('_')
}
function BuildHiddenVbs([string]$Path,[string]$Execute,[string]$Arguments,[string]$WorkingDirectory) {
    $Exe=[Environment]::ExpandEnvironmentVariables($Execute)
    $Args=if($Arguments){[Environment]::ExpandEnvironmentVariables($Arguments)}else{""}
    $Work=if($WorkingDirectory){[Environment]::ExpandEnvironmentVariables($WorkingDirectory)}else{""}
    $Command='"'+$Exe+'"'
    if(-not [string]::IsNullOrWhiteSpace($Args)){ $Command += " " + $Args }
    $Lines=@('Option Explicit','Dim sh, rc','Set sh = CreateObject("WScript.Shell")')
    if($Work -and (Test-Path $Work)){ $Lines += ('sh.CurrentDirectory = "{0}"' -f (VbsEscape $Work)) }
    $Lines += ('rc = sh.Run("{0}", 0, True)' -f (VbsEscape $Command))
    $Lines += 'Set sh = Nothing'
    $Lines | Set-Content $Path -Encoding ASCII
}
function ReadJson([string]$Path) {
    if(-not (Test-Path $Path)){ return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}
function ProcCount([string]$Needle) {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*$Needle*"
    }).Count
}
function Fail([string]$Reason,[object]$Details=$null) {
    $obj=[ordered]@{
        schema="TRADINGCORE_RUNTIME_REPAIR_V3"
        state="FAILED_SAFE_NO_TERMINAL_CLEANUP"
        reason=$Reason
        details=$Details
        updated_at_utc=(Get-Date).ToUniversalTime().ToString("o")
        real_orders_enabled=$false
        live_trading_enabled=$false
        collector_a_modified=$false
    }
    $obj | ConvertTo-Json -Depth 20 | Set-Content $StatusPath -Encoding UTF8
    Write-Host ""
    Write-Host "RUNTIME REPAIR V3 STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Reason -ForegroundColor Yellow
    Write-Host "Old Terminal windows were NOT closed." -ForegroundColor Yellow
    Write-Host "Status: $StatusPath"
    exit 1
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE RUNTIME REPAIR V3" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$Tasks=@(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -like "TradingCore*" })
if($Tasks.Count -eq 0){ Fail "No TradingCore Scheduled Tasks found." }

$BackupRows=@()
foreach($Task in $Tasks){
    foreach($Action in @($Task.Actions)){
        $BackupRows += [ordered]@{
            task_name=$Task.TaskName; task_path=$Task.TaskPath; state=[string]$Task.State
            execute=[string]$Action.Execute; arguments=[string]$Action.Arguments; working_directory=[string]$Action.WorkingDirectory
        }
    }
}
$BackupRows | ConvertTo-Json -Depth 10 | Set-Content $Backup -Encoding UTF8
Write-Host "Backup: $Backup" -ForegroundColor DarkGray

# Stop task instances before replacing action wrappers.
foreach($Task in $Tasks){
    if($Task.State -eq "Running"){
        Stop-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 3

# Kill only TradingCore worker/wrapper processes; evidence is append-only and
# all workers are restart-safe.
$Patterns=@(
    'TradingCore_PAPER','TradingCore_Collector','TradingCore_Autonomous','TradingCore_Wide_Autonomous',
    'TradingCore_Cloud_Recovery','btc_1h_forward_shadow','collector_b_','collector_c_',
    'forced_flow_','RUN_5H_OVERNIGHT_SUPERVISOR','TradingCore_HiddenLaunchers'
)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    if($_.ProcessId -eq $PID -or -not $_.CommandLine){ return $false }
    $cmd=[string]$_.CommandLine
    foreach($p in $Patterns){ if($cmd -match [regex]::Escape($p)){ return $true } }
    return $false
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$Migrated=0
foreach($Task0 in $Tasks){
    $Task=Get-ScheduledTask -TaskName $Task0.TaskName -TaskPath $Task0.TaskPath -ErrorAction Stop
    $Actions=@($Task.Actions)
    if($Actions.Count -ne 1){ Fail "Task $($Task.TaskName) has $($Actions.Count) actions; expected 1." }
    $Old=$Actions[0]; $OldExe=[string]$Old.Execute; $OldArgs=[string]$Old.Arguments

    if($OldExe -match '(?i)wscript\.exe$' -and $OldArgs -match 'TradingCore_HiddenLaunchers'){
        $Vbs=$OldArgs.Trim().Trim('"')
        if(-not (Test-Path $Vbs)){ Fail "Hidden launcher missing for $($Task.TaskName): $Vbs" }
        $Text=Get-Content $Vbs -Raw
        $NewText=$Text -replace ',\s*0\s*,\s*False\s*\)', ', 0, True)'
        if($NewText -ne $Text){ Set-Content $Vbs -Value $NewText -Encoding ASCII; $Migrated++ }
        Write-Host "Hidden V2: $($Task.TaskName)" -ForegroundColor Green
        continue
    }

    $Safe=SafeName ($Task.TaskPath.Trim('\')+'_'+$Task.TaskName)
    if([string]::IsNullOrWhiteSpace($Safe)){ $Safe=SafeName $Task.TaskName }
    $Vbs="$HiddenRoot\$Safe.vbs"
    BuildHiddenVbs $Vbs $OldExe $OldArgs ([string]$Old.WorkingDirectory)
    $NewAction=New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $Vbs)
    Set-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -Action $NewAction -ErrorAction Stop | Out-Null
    $Migrated++
    Write-Host "Hidden: $($Task.TaskName)" -ForegroundColor Green
}

# Start long-running services first. Periodic jobs retain their own schedule;
# run each once only to refresh health evidence.
foreach($Name in $LongRunning){
    $T=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if($T -and $T.State -ne "Disabled"){
        Start-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    }
}
foreach($Name in $Periodic){
    $T=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if($T -and $T.State -ne "Disabled"){
        Start-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    }
}

# Relaunch continuous keep-awake supervisor hidden and supervised.
if(Test-Path $ContinuousSupervisor){
    $SupVbs="$HiddenRoot\TradingCore_Continuous_Supervisor.vbs"
    $Ps="$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $Args='-NoProfile -ExecutionPolicy Bypass -File "'+$ContinuousSupervisor+'"'
    BuildHiddenVbs $SupVbs $Ps $Args $Repo
    Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ('"{0}"' -f $SupVbs) -WindowStyle Hidden
}

# Allow supervisors to launch child Python processes. Wide forward used to race
# UNIVERSE_LOCK creation; the lock now already exists from the partial install,
# and the launcher is self-healing even if its first child exited earlier.
Start-Sleep -Seconds 20

$TaskStates=[ordered]@{}
$TaskFailures=@()
foreach($Name in $LongRunning){
    $T=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if(-not $T){ $TaskStates[$Name]="MISSING"; $TaskFailures += "$Name:MISSING"; continue }
    $TaskStates[$Name]=[string]$T.State
    if($T.State -ne "Running"){ $TaskFailures += "$Name:$($T.State)" }
}
foreach($Name in $Periodic){
    $T=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if($T){ $TaskStates[$Name]=[string]$T.State }
}

$Health=[ordered]@{}
try {
    $Paper=Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5
    $Health.main_paper_ok=($Paper.running -eq $true -and $Paper.real_orders_enabled -eq $false)
} catch { $Health.main_paper_ok=$false }
$Shadow=ReadJson "C:\TradingCore_BTC_1H_SHADOW\status.json"
$Health.shadow_ok=($Shadow -and $Shadow.running -eq $true -and $Shadow.real_orders_enabled -eq $false)
$B=ReadJson "C:\TradingCore_Collector_B\data\status.json"
$Health.collector_b_ok=($B -and $B.running -eq $true -and $B.connection_state -eq "CONNECTED" -and $B.real_orders_enabled -eq $false)
$Health.v1_orchestrator_processes=ProcCount "forced_flow_autonomous_orchestrator.py"
$Health.v1_forward_processes=ProcCount "forced_flow_forward_paper.py"

$WideTask=Get-ScheduledTask -TaskName "TradingCore Collector C Wide" -ErrorAction SilentlyContinue
if($WideTask){
    $C=ReadJson "C:\TradingCore_Collector_C\data\status.json"
    $Universe=ReadJson "C:\TradingCore_Collector_C\data\UNIVERSE_LOCK.json"
    $WideStatus=ReadJson "C:\TradingCore_Wide_Autonomous\status.json"
    $WideForward=ReadJson "C:\TradingCore_Wide_Autonomous\forward_paper_status.json"
    $Health.collector_c_ok=($C -and $C.running -eq $true -and $C.connection_state -eq "CONNECTED" -and $C.real_orders_enabled -eq $false)
    $Health.wide_universe_locked=($Universe -and @($Universe.symbols).Count -gt 0)
    $Health.wide_orchestrator_processes=ProcCount "forced_flow_wide_autonomous_orchestrator.py"
    $Health.wide_forward_processes=ProcCount "forced_flow_wide_forward_paper.py"
    $Health.wide_orchestrator_state=if($WideStatus){$WideStatus.state}else{$null}
    $Health.wide_forward_state=if($WideForward){$WideForward.state}else{$null}
}

$CriticalFailures=@()
if(-not $Health.main_paper_ok){$CriticalFailures += "MAIN_PAPER"}
if(-not $Health.shadow_ok){$CriticalFailures += "BTC_SHADOW"}
if(-not $Health.collector_b_ok){$CriticalFailures += "COLLECTOR_B"}
if([int]$Health.v1_orchestrator_processes -lt 1){$CriticalFailures += "V1_ORCHESTRATOR"}
if([int]$Health.v1_forward_processes -lt 1){$CriticalFailures += "V1_FORWARD"}
if($WideTask){
    if(-not $Health.collector_c_ok){$CriticalFailures += "COLLECTOR_C"}
    if(-not $Health.wide_universe_locked){$CriticalFailures += "WIDE_UNIVERSE"}
    if([int]$Health.wide_orchestrator_processes -lt 1){$CriticalFailures += "WIDE_ORCHESTRATOR"}
    if([int]$Health.wide_forward_processes -lt 1){$CriticalFailures += "WIDE_FORWARD"}
}

$Final=[ordered]@{
    schema="TRADINGCORE_RUNTIME_REPAIR_V3"
    state=if($TaskFailures.Count -eq 0 -and $CriticalFailures.Count -eq 0){"PASS"}else{"FAILED_SAFE"}
    repaired_at_utc=(Get-Date).ToUniversalTime().ToString("o")
    migrated_or_upgraded_actions=$Migrated
    backup=$Backup
    long_running_task_failures=$TaskFailures
    critical_health_failures=$CriticalFailures
    task_states=$TaskStates
    health=$Health
    real_orders_enabled=$false
    live_trading_enabled=$false
    collector_a_modified=$false
}
$Final | ConvertTo-Json -Depth 20 | Set-Content $StatusPath -Encoding UTF8

if($TaskFailures.Count -gt 0 -or $CriticalFailures.Count -gt 0){
    Fail ("Runtime verification failed: tasks=["+($TaskFailures -join ',')+"] health=["+($CriticalFailures -join ',')+"]") $Final
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE RUNTIME V3: PASS" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "All long-running TradingCore services: RUNNING / HIDDEN" -ForegroundColor Green
Write-Host "Periodic audits: HIDDEN / scheduled"
Write-Host "Main PAPER: HEALTHY"
Write-Host "Collector B: CONNECTED"
if($WideTask){
    Write-Host "Collector C Wide: CONNECTED"
    Write-Host "Wide Orchestrator: RUNNING"
    Write-Host "Wide Forward PAPER: RUNNING / inert until historical PASS"
}
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Old Terminal windows will close in 5 seconds." -ForegroundColor Cyan

$Cleanup="$HiddenRoot\CLOSE_OLD_WINDOWS_TERMINAL_V3.vbs"
@'
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Sleep 5000
sh.Run "taskkill /F /IM WindowsTerminal.exe", 0, True
Set sh = Nothing
'@ | Set-Content $Cleanup -Encoding ASCII
Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ('"{0}"' -f $Cleanup) -WindowStyle Hidden
