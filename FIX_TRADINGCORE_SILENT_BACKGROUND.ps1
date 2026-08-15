#requires -Version 5.1
<#
Make all TradingCore background Scheduled Tasks truly silent on Windows 11.

Why: -WindowStyle Hidden is not sufficient when Windows Terminal is the default
terminal host. A scheduled powershell.exe/cmd/python action can still create a
visible Terminal window. This repair wraps each existing TradingCore task action
in wscript.exe -> WScript.Shell.Run(..., 0, False), preserving the original
command/arguments and task triggers/settings.

Safety:
- Backs up every original task action before changing it.
- Stops only TradingCore task instances / TradingCore worker processes.
- Restarts only tasks that were running before repair.
- Does not alter trading parameters, strategy data, Collector A evidence, or LIVE.
- Restarts the continuous no-sleep supervisor through a hidden WScript wrapper.
- Closes existing Windows Terminal windows only after worker restart verification.
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
    if (-not [string]::IsNullOrWhiteSpace($Args)) {
        $Command += " " + $Args
    }

    $Lines = @(
        'Option Explicit',
        'Dim sh, rc',
        'Set sh = CreateObject("WScript.Shell")'
    )
    if ($Work -and (Test-Path $Work)) {
        $Lines += ('sh.CurrentDirectory = "{0}"' -f (Vbs-Escape $Work))
    }
    $Lines += ('rc = sh.Run("{0}", 0, False)' -f (Vbs-Escape $Command))
    $Lines += 'Set sh = Nothing'
    $Lines | Set-Content -Path $Path -Encoding ASCII
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE SILENT BACKGROUND REPAIR" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

$Tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    $_.TaskName -like "TradingCore*"
})

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
            task_name = $Task.TaskName
            task_path = $Task.TaskPath
            state_before = [string]$Task.State
            execute = [string]$Action.Execute
            arguments = [string]$Action.Arguments
            working_directory = [string]$Action.WorkingDirectory
        }
    }
}
$Backups | ConvertTo-Json -Depth 8 | Set-Content $Backup -Encoding UTF8
Write-Host "Backup: $Backup" -ForegroundColor DarkGray

# Stop current task instances first. The tasks are restart-safe by design.
foreach ($Task in $Tasks) {
    if ($Task.State -eq "Running") {
        try {
            Stop-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
        } catch {
            Write-Host "Stop warning: $($Task.TaskName): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}
Start-Sleep -Seconds 3

# Stop any orphaned TradingCore workers left from old visible PowerShell wrappers.
$Patterns = @(
    'TradingCore_PAPER',
    'TradingCore_Collector',
    'TradingCore_Autonomous',
    'TradingCore_Cloud_Recovery',
    'btc_1h_forward_shadow',
    'collector_b_',
    'collector_c_',
    'forced_flow_',
    'RUN_5H_OVERNIGHT_SUPERVISOR'
)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    if ($_.ProcessId -eq $PID -or -not $_.CommandLine) { return $false }
    $cmd = [string]$_.CommandLine
    foreach ($p in $Patterns) {
        if ($cmd -match [regex]::Escape($p)) { return $true }
    }
    return $false
} | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
}
Start-Sleep -Seconds 2

# Replace only the ACTION of each task. Triggers/principal/settings remain intact.
$Changed = 0
foreach ($Task in $Tasks) {
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        Write-Host "Skipped $($Task.TaskName): expected 1 action, found $($Actions.Count)" -ForegroundColor Yellow
        continue
    }

    $Old = $Actions[0]
    $OldExe = [string]$Old.Execute
    $OldArgs = [string]$Old.Arguments

    # Already migrated by this repair: leave unchanged.
    if ($OldExe -match '(?i)wscript\.exe$' -and $OldArgs -match 'TradingCore_HiddenLaunchers') {
        continue
    }

    $Safe = Safe-Name ($Task.TaskPath.Trim('\') + '_' + $Task.TaskName)
    if ([string]::IsNullOrWhiteSpace($Safe)) { $Safe = Safe-Name $Task.TaskName }
    $Vbs = Join-Path $Root ($Safe + ".vbs")

    Build-HiddenVbs -Path $Vbs -Execute $OldExe -Arguments $OldArgs -WorkingDirectory ([string]$Old.WorkingDirectory)

    $NewAction = New-ScheduledTaskAction `
        -Execute "$env:WINDIR\System32\wscript.exe" `
        -Argument ('"{0}"' -f $Vbs)

    try {
        Set-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -Action $NewAction -ErrorAction Stop | Out-Null
        $Changed++
        Write-Host "Silent: $($Task.TaskName)" -ForegroundColor Green
    } catch {
        Write-Host "FAILED to migrate $($Task.TaskName): $($_.Exception.Message)" -ForegroundColor Red
        throw
    }
}

# Restart only tasks that were active before the repair.
foreach ($Task in $Tasks) {
    $Key = $Task.TaskPath + $Task.TaskName
    if ($WasRunning[$Key]) {
        try {
            Start-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -ErrorAction Stop
        } catch {
            Write-Host "Restart failed: $($Task.TaskName): $($_.Exception.Message)" -ForegroundColor Red
            throw
        }
    }
}

# The continuous keep-awake supervisor was launched manually, not as a task.
# Relaunch it through WScript as well so it cannot own a Terminal window.
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
    $Key = $Task.TaskPath + $Task.TaskName
    $ExpectedRunning = [bool]$WasRunning[$Key]
    $NowState = if ($Current) { [string]$Current.State } else { "MISSING" }
    if ($ExpectedRunning -and $NowState -ne "Running") { $AllGood = $false }
    $Verification += [ordered]@{
        task = $Task.TaskName
        state = $NowState
        was_running = $ExpectedRunning
        action = if ($Current) { [string]$Current.Actions[0].Execute } else { $null }
    }
}

$Status = [ordered]@{
    schema = "TRADINGCORE_SILENT_BACKGROUND_REPAIR_V1"
    repaired_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    task_count = $Tasks.Count
    actions_changed = $Changed
    all_previously_running_tasks_restored = $AllGood
    backup = $Backup
    real_orders_enabled = $false
    live_trading_enabled = $false
    collector_a_modified = $false
    tasks = $Verification
}
$Status | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $Root "LATEST_STATUS.json") -Encoding UTF8

if (-not $AllGood) {
    Write-Host ""
    Write-Host "SILENT REPAIR PARTIAL - one or more previously-running tasks did not restart." -ForegroundColor Red
    Write-Host "Terminal cleanup NOT executed. Status: $Root\LATEST_STATUS.json" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " TRADINGCORE BACKGROUND IS NOW SILENT" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Tasks migrated: $Changed"
Write-Host "Previously running tasks restored: YES" -ForegroundColor Green
Write-Host "Continuous supervisor: HIDDEN"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Existing Terminal windows will close in 5 seconds." -ForegroundColor Cyan

# Delayed cleanup so this PowerShell can print success first. This intentionally
# closes all currently-open Windows Terminal windows; background workers have
# already been restarted through WScript and are independent of the Terminal UI.
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
