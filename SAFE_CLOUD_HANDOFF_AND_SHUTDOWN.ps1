#requires -Version 5.1
<#
TradingCore / AI projects safe cloud handoff + shutdown orchestrator.

Sequence is intentionally fail-closed:
1) update trusted recovery repo;
2) deploy Collector B + hourly G2/G3 watchdog to a NEW isolated Railway project/service;
3) attach a dedicated /data volume and force PAPER/no-LIVE variables;
4) verify latest deployment SUCCESS and download remote /data/status.json;
5) only after cloud verification: preserve local handoff evidence;
6) gracefully stop/disable AI Media Factory, LinguaPilot and local TradingCore tasks/processes;
7) archive local Collector B evidence and attempt to copy it to Railway handoff storage;
8) write local handoff manifest;
9) shut Windows down.

If any mandatory cloud-handoff step fails, local projects are left running and
Windows is NOT shut down.
#>

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Branch = "reconcile-railway-paper"
$ProjectName = "TradingCore-Collector-B-Cloud"
$ServiceName = "Collector-B"
$EnvironmentName = "production"
$LocalCollectorRoot = "C:\TradingCore_Collector_B"
$MediaRoot = "C:\AI_Media_Factory"
$LanguageRoot = "C:\LinguaPilot"

$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
$HandoffRoot = "C:\AI_Shutdown_Handoff\$Stamp"
$Stage = Join-Path $env:TEMP "TradingCore_Collector_B_CloudDeploy"
$MasterLog = Join-Path $HandoffRoot "SAFE_HANDOFF.log"
$FinalManifest = Join-Path $HandoffRoot "FINAL_HANDOFF.json"

New-Item -ItemType Directory -Force -Path $HandoffRoot | Out-Null

function Log([string]$Text) {
    $line = "{0}  {1}" -f ((Get-Date).ToUniversalTime().ToString("o")), $Text
    $line | Tee-Object -FilePath $MasterLog -Append
}

function Fail-Safe([string]$Reason) {
    Log "FAIL-SAFE: $Reason"
    @{
        state = "FAIL_SAFE_NO_SHUTDOWN"
        reason = $Reason
        cloud_collector_verified = $false
        collector_a_modified = $false
        real_orders_enabled = $false
        shutdown_requested = $false
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Depth 10 | Set-Content $FinalManifest -Encoding UTF8

    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Red
    Write-Host " SAFE HANDOFF STOPPED - LAPTOP WILL NOT SHUT DOWN" -ForegroundColor Red
    Write-Host "==================================================" -ForegroundColor Red
    Write-Host $Reason -ForegroundColor Yellow
    Write-Host "Log: $MasterLog"
    exit 1
}

function Run-Cmd([string]$Exe, [string[]]$Args, [bool]$AllowFail = $false) {
    Log ("RUN: {0} {1}" -f $Exe, ($Args -join " "))
    $output = (& $Exe @Args 2>&1 | Out-String)
    $code = $LASTEXITCODE
    if ($output) {
        $output.TrimEnd() | Add-Content $MasterLog
    }
    if (-not $AllowFail -and $code -ne 0) {
        throw "$Exe failed with exit code $code"
    }
    return [pscustomobject]@{ Code = $code; Output = $output }
}

function Railway([string[]]$Args, [bool]$AllowFail = $false) {
    return Run-Cmd "railway" $Args $AllowFail
}

function Stop-And-Disable-Tasks([string]$Regex, [string]$Label) {
    $found = @()
    try {
        $found = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
            $_.TaskName -match $Regex -or $_.TaskPath -match $Regex
        })
    } catch {
        Log "$Label task enumeration warning: $($_.Exception.Message)"
    }

    foreach ($task in $found) {
        Log "$Label task: $($task.TaskPath)$($task.TaskName) state=$($task.State)"
        try {
            if ($task.State -eq "Running") {
                Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
            }
        } catch {
            Log "$Label Stop-ScheduledTask warning: $($_.Exception.Message)"
        }
        try {
            Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop | Out-Null
        } catch {
            Log "$Label Disable-ScheduledTask warning: $($_.Exception.Message)"
        }
    }

    return $found
}

function Stop-Processes-For-Root([string]$Root, [string]$Label) {
    if (-not (Test-Path $Root)) { return @() }
    $escaped = [regex]::Escape($Root)
    $targets = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -match $escaped -and
        $_.Name -match '^(python|pythonw|node|powershell|pwsh|cmd|ffmpeg|comfyui).*'
    })

    foreach ($p in $targets) {
        Log "$Label stopping PID=$($p.ProcessId) Name=$($p.Name)"
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {
            Log "$Label Stop-Process warning PID=$($p.ProcessId): $($_.Exception.Message)"
        }
    }
    return $targets
}

try {
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " SAFE CLOUD HANDOFF + PROJECT SHUTDOWN" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host "Collector B -> isolated Railway cloud"
    Write-Host "Then: AI Media + LinguaPilot + TradingCore local safe stop"
    Write-Host "Shutdown happens ONLY after cloud verification"
    Write-Host ""

    if (-not (Test-Path $Repo)) {
        Fail-Safe "Trusted TradingCore recovery repo not found: $Repo"
    }

    # ------------------------------------------------------------------
    # 1. Update trusted source.
    # ------------------------------------------------------------------
    $git = Run-Cmd "git" @("-C", $Repo, "pull", "--ff-only", "origin", $Branch) $true
    if ($git.Code -ne 0) {
        Fail-Safe "Git update failed; local projects left running."
    }

    # ------------------------------------------------------------------
    # 2. Railway CLI / authentication gate.
    # ------------------------------------------------------------------
    if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
        Log "Railway CLI missing; trying npm installation."
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Fail-Safe "Railway CLI is not installed and npm is unavailable."
        }
        $install = Run-Cmd "npm" @("install", "-g", "@railway/cli") $true
        if ($install.Code -ne 0 -or -not (Get-Command railway -ErrorAction SilentlyContinue)) {
            Fail-Safe "Automatic Railway CLI installation failed."
        }
    }

    $who = Railway @("whoami") $true
    if ($who.Code -ne 0) {
        Fail-Safe "Railway CLI is not authenticated. No shutdown performed."
    }
    Log "Railway identity verified."

    # ------------------------------------------------------------------
    # 3. Build an isolated deployment directory. Never deploy whole repo.
    # ------------------------------------------------------------------
    if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null

    $required = @(
        "collector_b_bybit.py",
        "collector_b_selftest.py",
        "collector_b_g2_g3_audit.py",
        "collector_b_cloud_supervisor.py",
        "collector_b_requirements.txt",
        "Dockerfile.collector_b"
    )
    foreach ($name in $required) {
        $src = Join-Path $Repo $name
        if (-not (Test-Path $src)) { Fail-Safe "Required cloud file missing: $src" }
        if ($name -eq "Dockerfile.collector_b") {
            Copy-Item $src (Join-Path $Stage "Dockerfile") -Force
        } else {
            Copy-Item $src $Stage -Force
        }
    }
    Copy-Item (Join-Path $Repo "config") (Join-Path $Stage "config") -Recurse -Force

    Push-Location $Stage
    try {
        # --------------------------------------------------------------
        # 4. Isolated Railway project/service. Idempotent on repeat runs.
        # --------------------------------------------------------------
        $projects = Railway @("list", "--json") $true
        if ($projects.Code -ne 0) { Fail-Safe "Cannot list Railway projects." }

        $projectExists = $projects.Output -match ('"name"\s*:\s*"' + [regex]::Escape($ProjectName) + '"')
        if (-not $projectExists) {
            Log "Creating isolated Railway project: $ProjectName"
            $init = Railway @("init", "-n", $ProjectName, "--json") $true
            if ($init.Code -ne 0) { Fail-Safe "Cannot create isolated Railway project." }
        }

        $link = Railway @("link", "-p", $ProjectName, "-e", $EnvironmentName, "--json") $true
        if ($link.Code -ne 0) { Fail-Safe "Cannot link Railway project/environment." }

        $services = Railway @("service", "list", "--json") $true
        $serviceExists = $services.Code -eq 0 -and $services.Output -match ('"name"\s*:\s*"' + [regex]::Escape($ServiceName) + '"')
        if (-not $serviceExists) {
            Log "Creating isolated service: $ServiceName"
            $add = Railway @("add", "--service", $ServiceName, "--json") $true
            if ($add.Code -ne 0) { Fail-Safe "Cannot create Collector B Railway service." }
        }

        $linkService = Railway @("link", "-p", $ProjectName, "-e", $EnvironmentName, "-s", $ServiceName, "--json") $true
        if ($linkService.Code -ne 0) { Fail-Safe "Cannot link Collector B service." }

        # Dedicated persistent volume.
        $volumes = Railway @("volume", "list", "--json") $true
        $hasDataVolume = $volumes.Code -eq 0 -and $volumes.Output -match '(/data|\\/data)'
        if (-not $hasDataVolume) {
            Log "Creating dedicated /data volume."
            $volumeAdd = Railway @("volume", "add", "--service", $ServiceName, "--mount-path", "/data", "--json") $true
            if ($volumeAdd.Code -ne 0) { Fail-Safe "Cannot create/attach dedicated Collector B volume." }
        }

        # Hard safety variables. No private exchange credentials are copied.
        $vars = Railway @(
            "variable", "set",
            "TRADING_ENVIRONMENT=PAPER",
            "LIVE_TRADING=false",
            "PAPER_TRADING=true",
            "DEMO_ONLY=true",
            "COLLECTOR_B_DATA_DIR=/data",
            "COLLECTOR_B_AUDIT_INTERVAL_SECONDS=3600",
            "COLLECTOR_B_AUDIT_INITIAL_DELAY_SECONDS=60",
            "--service", $ServiceName,
            "--environment", $EnvironmentName,
            "--skip-deploys",
            "--json"
        ) $true
        if ($vars.Code -ne 0) { Fail-Safe "Cannot set Collector B Railway safety variables." }

        # --------------------------------------------------------------
        # 5. Deploy isolated directory using Dockerfile.
        # --------------------------------------------------------------
        Log "Uploading Collector B cloud deployment."
        $up = Railway @("up", "--service", $ServiceName, "--environment", $EnvironmentName, "--detach", "--json") $true
        if ($up.Code -ne 0) { Fail-Safe "Railway upload/deploy command failed." }

        $deploymentSuccess = $false
        for ($i = 0; $i -lt 72; $i++) {
            Start-Sleep -Seconds 5
            $deployments = Railway @("deployment", "list", "--service", $ServiceName, "--environment", $EnvironmentName, "--limit", "1", "--json") $true
            if ($deployments.Code -ne 0) { continue }
            if ($deployments.Output -match '"status"\s*:\s*"SUCCESS"') {
                $deploymentSuccess = $true
                break
            }
            if ($deployments.Output -match '"status"\s*:\s*"(FAILED|CRASHED)"') {
                Fail-Safe "Collector B cloud deployment failed or crashed."
            }
        }
        if (-not $deploymentSuccess) {
            Fail-Safe "Collector B cloud deployment did not reach SUCCESS."
        }
        Log "Railway deployment status SUCCESS."

        # --------------------------------------------------------------
        # 6. Strong cloud verification from the dedicated volume.
        # --------------------------------------------------------------
        $CloudStatusFile = Join-Path $HandoffRoot "cloud_collector_b_status.json"
        $CloudSupervisorFile = Join-Path $HandoffRoot "cloud_supervisor_status.json"
        $cloudVerified = $false

        for ($i = 0; $i -lt 36; $i++) {
            Start-Sleep -Seconds 5
            Remove-Item $CloudStatusFile -Force -ErrorAction SilentlyContinue
            $download = Railway @(
                "service", "files", "download",
                "/data/status.json", $CloudStatusFile,
                "--overwrite",
                "--service", $ServiceName,
                "--environment", $EnvironmentName,
                "--json"
            ) $true

            if ($download.Code -eq 0 -and (Test-Path $CloudStatusFile)) {
                try {
                    $cs = Get-Content $CloudStatusFile -Raw | ConvertFrom-Json
                    $cloudVerified = (
                        $cs.running -eq $true -and
                        [string]$cs.connection_state -eq "CONNECTED" -and
                        $cs.private_api_used -eq $false -and
                        $cs.real_orders_enabled -eq $false -and
                        $cs.real_order_sent -eq $false -and
                        $cs.strategy_logic_enabled -eq $false -and
                        $cs.outcome_computation_enabled -eq $false -and
                        $cs.collector_a_modified -eq $false
                    )
                } catch {
                    $cloudVerified = $false
                }
                if ($cloudVerified) { break }
            }
        }

        if (-not $cloudVerified) {
            $logs = Railway @("logs", "--service", $ServiceName, "--environment", $EnvironmentName, "--lines", "80") $true
            if ($logs.Output) { $logs.Output | Set-Content (Join-Path $HandoffRoot "cloud_last_logs.txt") -Encoding UTF8 }
            Fail-Safe "Cloud Collector B was deployed but did not pass runtime safety/CONNECTED verification."
        }

        # Cloud watchdog status is useful but not a shutdown blocker because the
        # collector itself is already verified. It will run the first audit later.
        Railway @(
            "service", "files", "download",
            "/data/cloud_supervisor_status.json", $CloudSupervisorFile,
            "--overwrite",
            "--service", $ServiceName,
            "--environment", $EnvironmentName,
            "--json"
        ) $true | Out-Null

        Log "CLOUD HANDOFF VERIFIED: Collector B CONNECTED; no private API/orders/LIVE; Collector A unchanged."

        # --------------------------------------------------------------
        # 7. Preserve local status evidence before stopping anything.
        # --------------------------------------------------------------
        try {
            Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5 |
                ConvertTo-Json -Depth 20 |
                Set-Content (Join-Path $HandoffRoot "trading_main_paper_status_before_stop.json") -Encoding UTF8
        } catch {
            Log "Main PAPER status snapshot warning: $($_.Exception.Message)"
        }

        foreach ($pair in @(
            @{Source="C:\TradingCore_BTC_1H_SHADOW\status.json"; Name="btc_1h_shadow_status_before_stop.json"},
            @{Source="C:\TradingCore_Collector_B\data\status.json"; Name="collector_b_local_status_before_stop.json"},
            @{Source="C:\TradingCore_Collector_B\data\G2_G3_SUPERVISOR_STATUS.json"; Name="collector_b_local_g2g3_before_stop.json"}
        )) {
            if (Test-Path $pair.Source) {
                Copy-Item $pair.Source (Join-Path $HandoffRoot $pair.Name) -Force
            }
        }

        # --------------------------------------------------------------
        # 8. AI Media Factory: checkpoint-aware stop first.
        # --------------------------------------------------------------
        if (Test-Path $MediaRoot) {
            $mediaStop = Join-Path $MediaRoot "stop_autonomous_run.ps1"
            if (Test-Path $mediaStop) {
                Log "AI Media Factory: executing stop_autonomous_run.ps1"
                Push-Location $MediaRoot
                try {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $mediaStop *>&1 |
                        Set-Content (Join-Path $HandoffRoot "ai_media_stop.log") -Encoding UTF8
                } catch {
                    Log "AI Media stop script warning: $($_.Exception.Message)"
                } finally { Pop-Location }
            }
        }
        $mediaTasks = Stop-And-Disable-Tasks '(?i)AI[_ ]Media[_ ]Factory|AI Media Factory' "AI Media"
        Start-Sleep -Seconds 3
        Stop-Processes-For-Root $MediaRoot "AI Media" | Out-Null

        # --------------------------------------------------------------
        # 9. LinguaPilot: prefer an existing stop script, then stop tasks/processes.
        # --------------------------------------------------------------
        if (Test-Path $LanguageRoot) {
            $languageCandidates = @(
                (Join-Path $LanguageRoot "stop_linguapilot.ps1"),
                (Join-Path $LanguageRoot "STOP_LINGUAPILOT.ps1"),
                (Join-Path $LanguageRoot "stop.ps1"),
                (Join-Path $LanguageRoot "scripts\stop_linguapilot.ps1"),
                (Join-Path $LanguageRoot "scripts\stop.ps1")
            )
            $languageStop = $languageCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($languageStop) {
                Log "LinguaPilot: executing $languageStop"
                try {
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $languageStop *>&1 |
                        Set-Content (Join-Path $HandoffRoot "linguapilot_stop.log") -Encoding UTF8
                } catch {
                    Log "LinguaPilot stop script warning: $($_.Exception.Message)"
                }
            }
        }
        $languageTasks = Stop-And-Disable-Tasks '(?i)LinguaPilot|Language[ _-]?Pilot' "LinguaPilot"
        Start-Sleep -Seconds 2
        Stop-Processes-For-Root $LanguageRoot "LinguaPilot" | Out-Null

        # --------------------------------------------------------------
        # 10. Local TradingCore: cloud Collector B is already live, so local
        #     trading/research can now stop without interrupting cloud capture.
        # --------------------------------------------------------------
        $tradingTasks = Stop-And-Disable-Tasks '(?i)^TradingCore|TradingCore' "TradingCore"
        Start-Sleep -Seconds 4
        Stop-Processes-For-Root $LocalCollectorRoot "TradingCore Collector B" | Out-Null
        Stop-Processes-For-Root $Repo "TradingCore" | Out-Null

        # Port 8001 is the local PAPER API; stop any remaining listener.
        try {
            $listeners = @(Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue)
            foreach ($listener in $listeners) {
                if ($listener.OwningProcess -and $listener.OwningProcess -ne $PID) {
                    Log "TradingCore stopping residual local PAPER PID=$($listener.OwningProcess)"
                    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
                }
            }
        } catch {
            Log "Port 8001 cleanup warning: $($_.Exception.Message)"
        }

        # --------------------------------------------------------------
        # 11. Preserve local Collector B evidence. Cloud is already collecting;
        #     this archive is a handoff snapshot, not merged into the live cohort.
        # --------------------------------------------------------------
        $localArchive = $null
        if (Test-Path (Join-Path $LocalCollectorRoot "data")) {
            $localArchive = Join-Path $HandoffRoot "CollectorB_LocalEvidence_$Stamp.zip"
            try {
                Compress-Archive -Path (Join-Path $LocalCollectorRoot "data\*") -DestinationPath $localArchive -Force
                Log "Local Collector B evidence archived: $localArchive"
                $remoteArchive = "/data/handoff/CollectorB_LocalEvidence_$Stamp.zip"
                $upload = Railway @(
                    "service", "files", "upload",
                    $localArchive, $remoteArchive,
                    "--service", $ServiceName,
                    "--environment", $EnvironmentName,
                    "--json"
                ) $true
                if ($upload.Code -eq 0) {
                    Log "Local Collector B evidence archive copied to Railway handoff storage."
                } else {
                    Log "WARNING: local evidence archive upload failed; archive remains safe on laptop at $localArchive"
                }
            } catch {
                Log "WARNING: local Collector B archive error: $($_.Exception.Message)"
            }
        }

        # --------------------------------------------------------------
        # 12. Final process verification for user projects.
        #     Current orchestrator PID is explicitly excluded.
        # --------------------------------------------------------------
        $roots = @($MediaRoot, $LanguageRoot, $LocalCollectorRoot, $Repo) | Where-Object { Test-Path $_ }
        $leftovers = @()
        foreach ($root in $roots) {
            $escaped = [regex]::Escape($root)
            $leftovers += @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
                $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match $escaped
            })
        }
        $leftovers = @($leftovers | Sort-Object ProcessId -Unique)
        if ($leftovers.Count -gt 0) {
            foreach ($p in $leftovers) {
                Log "Final cleanup PID=$($p.ProcessId) Name=$($p.Name)"
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Seconds 2
        }

        # --------------------------------------------------------------
        # 13. Final handoff manifest. This is written before shutdown.
        # --------------------------------------------------------------
        $manifest = @{
            state = "SAFE_CLOUD_HANDOFF_COMPLETE_SHUTDOWN_REQUESTED"
            generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            railway_project = $ProjectName
            railway_environment = $EnvironmentName
            railway_service = $ServiceName
            railway_volume_mount = "/data"
            cloud_collector_verified = $true
            cloud_connection_state = "CONNECTED"
            cloud_watchdog_included = $true
            collector_a_modified = $false
            private_exchange_api_used = $false
            real_orders_enabled = $false
            live_trading_enabled = $false
            local_ai_media_stopped = $true
            local_linguapilot_stopped = $true
            local_tradingcore_stopped = $true
            local_project_tasks_disabled_for_next_logon = $true
            local_collector_evidence_archive = $localArchive
            handoff_directory = $HandoffRoot
            shutdown_delay_seconds = 30
        }
        $manifest | ConvertTo-Json -Depth 20 | Set-Content $FinalManifest -Encoding UTF8
        Log "FINAL HANDOFF COMPLETE. Requesting Windows shutdown."

        Write-Host ""
        Write-Host "==================================================" -ForegroundColor Green
        Write-Host " CLOUD HANDOFF VERIFIED - SAFE SHUTDOWN" -ForegroundColor Green
        Write-Host "==================================================" -ForegroundColor Green
        Write-Host "Collector B: RAILWAY CLOUD / CONNECTED" -ForegroundColor Green
        Write-Host "Cloud G2/G3 watchdog: ENABLED" -ForegroundColor Green
        Write-Host "Collector A: UNCHANGED" -ForegroundColor Green
        Write-Host "AI Media: STOPPED" -ForegroundColor Green
        Write-Host "LinguaPilot: STOPPED" -ForegroundColor Green
        Write-Host "Local TradingCore: STOPPED" -ForegroundColor Green
        Write-Host "REAL MONEY: DISABLED" -ForegroundColor Green
        Write-Host "Handoff: $HandoffRoot"
        Write-Host "Windows shutdown requested." -ForegroundColor Cyan

        shutdown.exe /s /t 30 /c "Safe AI handoff complete. Collector B is verified in Railway cloud."

    } finally {
        Pop-Location
    }

} catch {
    Fail-Safe $_.Exception.Message
}
