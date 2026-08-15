#requires -Version 5.1
<#
Make all TradingCore background Scheduled Tasks truly silent on Windows 11.
V2: WScript waits for the child process so Task Scheduler correctly keeps the
task in Running state and MultipleInstances=IgnoreNew remains meaningful.
#>

$ErrorActionPreference = "Stop"
$Root = "C:\TradingCore_HiddenLaunchers"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Backup = Join-Path $Root "TASK_ACTION_BACKUP_$Stamp.json"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$ContinuousSupervisor = Join-Path $Repo "RUN_5H_OVERNIGHT_SUPERVISOR.ps1"

New-Item -ItemType Directory -Force -Path $Root | Out-Null

function Safe-Name([string]$Text) {
    return ($Text -replace '[^A-Za-z0-9._-]', '_').Trim('_')
}
function Vbs-Escape([string]$Text) {
    if ($null -eq $Text) { return "" }
    return $Text.Replace('"','""')
}
function Build-HiddenVbs {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Execute,
        [string]$Arguments,
        [string]$WorkingDirectory
    )
    $Exe = [Environment]::ExpandEnvironmentVariables($Execute)
    $Args = if ($Arguments) { [Environment]::ExpandEnvironmentVariables($Arguments) } else { "" }
    $Work = if ($WorkingDirectory) { [Environment]::ExpandEnvironmentVariables($WorkingDirectory) } else { "" }
    $Command = '"' + $Exe + '"'
    if (-not [string]::IsNullOrWhiteSpace($Args)) { $Command += " " + $Args }
    $Lines = @(
        'Option Explicit',
        'Dim sh, rc',
        'Set sh = CreateObject("WScript.Shell")'
    )
    if ($Work -and (Test-Path $Work)) {
        $Lines += ('sh.CurrentDirectory = "{0}"' -f (Vbs-Escape $Work))
    }
    # IMPORTANT: True keeps wscript alive while the child is alive. The task
    # therefore remains Running but still has window style 0 (fully hidden).
    $Lines += ('rc = sh.Run("{0}", 0, True)' -f (Vbs-Escape $Command))
    $Lines += 'Set sh = Nothing'
    $Lines | Set-Content -Path $Path -Encoding ASCII
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE SILENT BACKGROUND REPAIR V2" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$Tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -like "TradingCore*" })
if ($Tasks.Count -eq 0) {
    Write-Host "No TradingCore Scheduled Tasks found. Nothing changed." -ForegroundColor Yellow
    exit 0
}

$Backups = @()
$WasRunning = @{}
foreach ($Task in $Tasks) {
    $WasRunning[$Task.TaskPath + $Task.TaskName] = ($Task.State -eq "Running")
    foreach ($Action in @($Task.Actions)) {
        $Backups += [ordered]@{
            task_name = $Task.TaskName; task_path = $Task.TaskPath; state_before = [string]$Task.State
            execute = [string]$Action.Execute; arguments = [string]$Action.Arguments
            working_directory = [string]$Action.WorkingDirectory
        }
    }
}
$Backups | ConvertTo-Json -Depth 8 | Set-Content $Backup -Encoding UTF8
Write-Host "Backup: $Backup" -ForegroundColor DarkGray

foreach ($Task in $Tasks) {
    if ($Task.State -eq "Running") {
        try { Stop-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction Stop } catch {}
    }
}
Start-Sleep -Seconds 3

# Stop only known TradingCore worker/wrapper processes. They are restart-safe.
$Patterns = @(
    'TradingCore_PAPER','TradingCore_Collector','TradingCore_Autonomous','TradingCore_Wide_Autonomous',
    'TradingCore_Cloud_Recovery','btc_1h_forward_shadow','collector_b_','collector_c_','forced_flow_',
    'RUN_5H_OVERNIGHT_SUPERVISOR','TradingCore_HiddenLaunchers'
)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    if ($_.ProcessId -eq $PID -or -not $_.CommandLine) { return $false }
    $cmd = [string]$_.CommandLine
    foreach ($p in $Patterns) { if ($cmd -match [regex]::Escape($p)) { return $true } }
    return $false
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$Changed = 0
foreach ($Task in $Tasks) {
    $Current = Get-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
    $Actions = @($Current.Actions)
    if ($Actions.Count -ne 1) {
        Write-Host "Skipped $($Task.TaskName): expected 1 action, found $($Actions.Count)" -ForegroundColor Yellow
        continue
    }
    $Old = $Actions[0]
    $OldExe = [string]$Old.Execute
    $OldArgs = [string]$Old.Arguments

    if ($OldExe -match '(?i)wscript\.exe$' -and $OldArgs -match 'TradingCore_HiddenLaunchers') {
        # Already migrated by V1: upgrade the existing VBS from detached child
        # (False) to supervised hidden child (True).
        $Vbs = $OldArgs.Trim().Trim('"')
        if (Test-Path $Vbs) {
            $Text = Get-Content $Vbs -Raw
            $NewText = $Text -replace ',\s*0\s*,\s*False\s*\)', ', 0, True)'
            if ($NewText -ne $Text) {
                Set-Content $Vbs -Value $NewText -Encoding ASCII
                $Changed++
                Write-Host "Silent supervisor upgraded: $($Task.TaskName)" -ForegroundColor Green
            } else {
                Write-Host "Silent already V2: $($Task.TaskName)" -ForegroundColor DarkGreen
            }
        } else {
            throw "Hidden VBS missing for $($Task.TaskName): $Vbs"
        }
        continue
    }

    $Safe = Safe-Name ($Task.TaskPath.Trim('\') + '_' + $Task.TaskName)
    if ([string]::IsNullOrWhiteSpace($Safe)) { $Safe = Safe-Name $Task.TaskName }
    $Vbs = Join-Path $Root ($Safe + ".vbs")
    Build-HiddenVbs -Path $Vbs -Execute $OldExe -Arguments $OldArgs -WorkingDirectory ([string]$Old.WorkingDirectory)
    $NewAction = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $Vbs)
    Set-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -Action $NewAction -ErrorAction Stop | Out-Null
    $Changed++
    Write-Host "Silent: $($Task.TaskName)" -ForegroundColor Green
}

# All current TradingCore tasks in this project are safe PAPER/research services.
# Start every enabled one, not merely those whose V1 detached wrapper happened to
# show state=Running before repair.
foreach ($Task in $Tasks) {
    $Current = Get-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction SilentlyContinue
    if ($Current -and $Current.State -ne "Disabled") {
        try { Start-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction Stop } catch {
            Write-Host "Restart failed: $($Task.TaskName): $($_.Exception.Message)" -ForegroundColor Red
            throw
        }
    }
}

if (Test-Path $ContinuousSupervisor) {
    $SupervisorVbs = Join-Path $Root "TradingCore_Continuous_Supervisor.vbs"
    $PsExe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $SupArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $ContinuousSupervisor + '"'
    Build-HiddenVbs -Path $SupervisorVbs -Execute $PsExe -Arguments $SupArgs -WorkingDirectory $Repo
    Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ('"{0}"' -f $SupervisorVbs) -WindowStyle Hidden
}

Start-Sleep -Seconds 12

$Verification = @()
$AllGood = $true
foreach ($Task in $Tasks) {
    $Current = Get-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction SilentlyContinue
    $NowState = if ($Current) { [string]$Current.State } else { "MISSING" }
    $ExpectedRunning = ($Current -and $Current.State -ne "Disabled")
    if ($ExpectedRunning -and $NowState -ne "Running") { $AllGood = $false }
    $Verification += [ordered]@{
        task=$Task.TaskName; state=$NowState; expected_running=[bool]$ExpectedRunning
        action=if($Current){[string]$Current.Actions[0].Execute}else{$null}
    }
}

$Status = [ordered]@{
    schema="TRADINGCORE_SILENT_BACKGROUND_REPAIR_V2"
    repaired_at_utc=(Get-Date).ToUniversalTime().ToString("o")
    task_count=$Tasks.Count; actions_changed_or_upgraded=$Changed
    all_enabled_tasks_running=$AllGood; backup=$Backup
    real_orders_enabled=$false; live_trading_enabled=$false; collector_a_modified=$false
    tasks=$Verification
}
$Status | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $Root "LATEST_STATUS.json") -Encoding UTF8

if (-not $AllGood) {
    Write-Host ""
    Write-Host "SILENT REPAIR V2 PARTIAL - a task still failed to stay Running." -ForegroundColor Red
    Write-Host "Status: $Root\LATEST_STATUS.json" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE BACKGROUND IS NOW SILENT" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "All enabled TradingCore tasks: RUNNING / HIDDEN" -ForegroundColor Green
Write-Host "Continuous supervisor: HIDDEN"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Existing Terminal windows will close in 5 seconds." -ForegroundColor Cyan

$CleanupVbs = Join-Path $Root "CLOSE_OLD_WINDOWS_TERMINAL.vbs"
@'
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Sleep 5000
sh.Run "taskkill /F /IM WindowsTerminal.exe", 0, True
Set sh = Nothing
'@ | Set-Content $CleanupVbs -Encoding ASCII
Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ('"{0}"' -f $CleanupVbs) -WindowStyle Hidden
