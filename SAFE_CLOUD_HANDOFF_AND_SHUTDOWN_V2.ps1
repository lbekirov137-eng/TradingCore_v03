#requires -Version 5.1
<#
Bootstrap V2 for SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1.

The caller refreshes only the required handoff files from origin before
launching this bootstrap. V2 performs NO second git pull. It creates a temporary
runtime copy of the audited orchestrator and applies narrowly-scoped safety and
current-Railway-CLI compatibility repairs before syntax-validating and running
it.

Repairs applied in the temporary runtime copy only:
1) remove the redundant second git pull;
2) make native command exit-code capture reliable on Windows PowerShell 5.1;
3) create the volume using the already-linked service, avoiding service-name vs
   service-ID ambiguity in `railway volume add`;
4) use `railway up --detach` without `--json` because current Railway docs state
   that --json implies CI mode.

All fail-closed cloud-verification and shutdown gates remain intact.
#>

$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Original = Join-Path $Repo "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1"
$Temp = Join-Path $env:TEMP "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN_RUNTIME.ps1"

$Required = @(
    "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1",
    "collector_b_bybit.py",
    "collector_b_selftest.py",
    "collector_b_g2_g3_audit.py",
    "collector_b_cloud_supervisor.py",
    "collector_b_requirements.txt",
    "Dockerfile.collector_b"
)

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " SAFE CLOUD HANDOFF BOOTSTRAP V2" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "Required files were refreshed from origin by the outer command."
Write-Host "No second git pull will be performed."
Write-Host ""

if (-not (Test-Path $Repo)) {
    Write-Host "TRUSTED REPOSITORY NOT FOUND - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}

foreach ($Name in $Required) {
    $Path = Join-Path $Repo $Name
    if (-not (Test-Path $Path)) {
        Write-Host "REQUIRED FILE MISSING: $Path" -ForegroundColor Red
        Write-Host "NO PROJECTS WERE STOPPED. NO SHUTDOWN." -ForegroundColor Yellow
        exit 1
    }
}

# Verify that the cloud supervisor is the Docker entrypoint before touching the
# original orchestrator text.
$Docker = Get-Content (Join-Path $Repo "Dockerfile.collector_b") -Raw
if ($Docker -notmatch 'collector_b_cloud_supervisor\.py') {
    Write-Host "CLOUD SUPERVISOR IS NOT IN DOCKERFILE - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}

$Text = Get-Content $Original -Raw
$Patched = $Text

# ---------------------------------------------------------------------------
# Repair A: robust native-command execution for Windows PowerShell 5.1.
# Capture $LASTEXITCODE immediately after the native command and before any
# PowerShell formatting pipeline. Temporarily use Continue so native stderr
# (git progress, CLI notices) doesn't become a terminating PS error.
# ---------------------------------------------------------------------------
$RunCmdPattern = '(?ms)^function Run-Cmd\(\[string\]\$Exe, \[string\[\]\]\$Args, \[bool\]\$AllowFail = \$false\) \{.*?^\}\r?\n\r?\nfunction Railway'
$RunCmdReplacement = @'
function Run-Cmd([string]$Exe, [string[]]$Args, [bool]$AllowFail = $false) {
    Log ("RUN: {0} {1}" -f $Exe, ($Args -join " "))

    $previousEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $outputLines = @(& $Exe @Args 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEap
    }

    $output = ($outputLines | Out-String)
    if ($output) {
        $output.TrimEnd() | Add-Content $MasterLog
    }

    if (-not $AllowFail -and $code -ne 0) {
        throw "$Exe failed with exit code $code"
    }

    return [pscustomobject]@{ Code = $code; Output = $output }
}

function Railway
'@

$Next = [regex]::Replace($Patched, $RunCmdPattern, $RunCmdReplacement, 1)
if ($Next -eq $Patched) {
    Write-Host "COULD NOT PATCH NATIVE COMMAND RUNNER - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}
$Patched = $Next

# ---------------------------------------------------------------------------
# Repair B: remove exactly the redundant internal git update. The outer command
# refreshes the required files using fetch + checkout from origin.
# ---------------------------------------------------------------------------
$GitPattern = '(?ms)^\s*# ------------------------------------------------------------------\r?\n\s*# 1\. Update trusted source\.\r?\n\s*# ------------------------------------------------------------------\r?\n\s*\$git = Run-Cmd "git" @\("-C", \$Repo, "pull", "--ff-only", "origin", \$Branch\) \$true\r?\n\s*if \(\$git\.Code -ne 0\) \{\r?\n\s*Fail-Safe "Git update failed; local projects left running\."\r?\n\s*\}\r?\n'
$GitReplacement = @'
    # ------------------------------------------------------------------
    # 1. Trusted source already refreshed by SAFE bootstrap V2.
    # ------------------------------------------------------------------
    Log "Required handoff files already refreshed from origin by bootstrap V2."

'@

$Next = [regex]::Replace($Patched, $GitPattern, $GitReplacement, 1)
if ($Next -eq $Patched) {
    Write-Host "COULD NOT SAFELY REMOVE REDUNDANT GIT STEP - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}
$Patched = $Next

# ---------------------------------------------------------------------------
# Repair C: current Railway CLI volume add can target the linked service. This
# avoids ambiguity because `railway volume` documents --service as a service ID.
# ---------------------------------------------------------------------------
$OldVolumeAdd = '$volumeAdd = Railway @("volume", "add", "--service", $ServiceName, "--mount-path", "/data", "--json") $true'
$NewVolumeAdd = '$volumeAdd = Railway @("volume", "add", "--mount-path", "/data", "--json") $true'
if (-not $Patched.Contains($OldVolumeAdd)) {
    Write-Host "VOLUME COMMAND SIGNATURE NOT FOUND - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}
$Patched = $Patched.Replace($OldVolumeAdd, $NewVolumeAdd)

# ---------------------------------------------------------------------------
# Repair D: current Railway docs state --json implies CI mode. Detached upload
# should use --detach without --json, then deployment list is polled as JSON.
# ---------------------------------------------------------------------------
$OldUp = '$up = Railway @("up", "--service", $ServiceName, "--environment", $EnvironmentName, "--detach", "--json") $true'
$NewUp = '$up = Railway @("up", "--service", $ServiceName, "--environment", $EnvironmentName, "--detach") $true'
if (-not $Patched.Contains($OldUp)) {
    Write-Host "RAILWAY UP SIGNATURE NOT FOUND - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}
$Patched = $Patched.Replace($OldUp, $NewUp)

Set-Content $Temp -Value $Patched -Encoding UTF8

# Parse-only syntax validation before executing anything consequential.
$Tokens = $null
$Errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $Temp,
    [ref]$Tokens,
    [ref]$Errors
)

if ($Errors.Count -gt 0) {
    Write-Host "ORCHESTRATOR SYNTAX CHECK FAILED - NO SHUTDOWN" -ForegroundColor Red
    $Errors | ForEach-Object { Write-Host $_.Message -ForegroundColor Yellow }
    exit 1
}

Write-Host "Bootstrap validation: PASS" -ForegroundColor Green
Write-Host "Starting fail-closed cloud handoff..." -ForegroundColor Cyan
Write-Host ""

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Temp
exit $LASTEXITCODE
