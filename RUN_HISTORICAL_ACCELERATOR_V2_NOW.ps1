#requires -Version 5.1
<# Historical Accelerator V2 NOW. V1 remains sealed. No terminal cleanup. #>
$ErrorActionPreference = "Stop"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root="C:\TradingCore_Historical_Accelerator_V2"
$Hidden="C:\TradingCore_HiddenLaunchers"
$Task="TradingCore Historical V2 Forward PAPER"
$Vbs="$Hidden\TradingCore_Historical_V2_Forward.vbs"
function Fail([string]$Text){Write-Host "";Write-Host "HISTORICAL V2 STOPPED SAFELY" -ForegroundColor Red;Write-Host $Text -ForegroundColor Yellow;Write-Host "Existing V1/Wide collectors continue. LIVE / real orders DISABLED." -ForegroundColor Green;exit 1}
Write-Host "";Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host " TRADINGCORE HISTORICAL ACCELERATOR V2 - RUN NOW" -ForegroundColor Cyan;Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host "Independent price-only V2. Reuses cached 730-day public price history.";Write-Host "V1 decision remains SEALED. No terminal cleanup. No real orders."
if(-not(Test-Path $Repo)){Fail "Repo missing"};if(-not(Test-Path $Py)){Fail "Python missing"};New-Item -ItemType Directory -Force -Path $Root,$Hidden|Out-Null
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true"
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY")|%{Remove-Item "Env:$_" -ErrorAction SilentlyContinue}
Push-Location $Repo
try{
  Write-Host "Compile + safety self-test..." -ForegroundColor Cyan
  & $Py -m py_compile .\historical_accelerator_v2_protocol.py .\historical_accelerator_v2.py .\historical_accelerator_v2_selftest.py .\historical_accelerator_v2_forward_paper.py
  if($LASTEXITCODE -ne 0){Fail "Python compile failed"}
  & $Py .\historical_accelerator_v2_selftest.py
  if($LASTEXITCODE -ne 0){Fail "V2 self-test failed"}
  Write-Host "";Write-Host "Running pre-final selection + sealed final holdout..." -ForegroundColor Cyan
  & $Py .\historical_accelerator_v2.py --state-dir $Root
  if($LASTEXITCODE -ne 0){Fail "Historical V2 engine returned non-zero"}
}finally{Pop-Location}
$Decision="$Root\HISTORICAL_V2_DECISION_LOCK.json";$Report="$Root\LATEST_HISTORICAL_V2.json"
if(-not(Test-Path $Decision)){Fail "V2 decision lock not created"}
$D=Get-Content $Decision -Raw|ConvertFrom-Json
$Candidate="$Root\CANDIDATE_FOR_FORWARD_PAPER.json"
if(Test-Path $Candidate){
  $Launcher="$Root\START_FORWARD_PAPER.ps1"
@'
$ErrorActionPreference="Continue"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root="C:\TradingCore_Historical_Accelerator_V2"
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true"
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET")|%{Remove-Item "Env:$_" -ErrorAction SilentlyContinue}
Set-Location $Repo
while($true){& $Py .\historical_accelerator_v2_forward_paper.py --state-dir $Root --poll-seconds 60 >> "$Root\forward_paper.log" 2>&1;Start-Sleep -Seconds 30}
'@|Set-Content $Launcher -Encoding UTF8
  $Ps="$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe";$Cmd='"'+$Ps+'" -NoProfile -ExecutionPolicy Bypass -File "'+$Launcher+'"';$Esc=$Cmd.Replace('"','""')
  @("Option Explicit","Dim sh, rc",'Set sh = CreateObject("WScript.Shell")',('rc = sh.Run("{0}", 0, True)' -f $Esc),'Set sh = Nothing')|Set-Content $Vbs -Encoding ASCII
  $Existing=Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue;if($Existing){Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue;Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue}
  $User=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;$Action=New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument ('"{0}"' -f $Vbs);$Trigger=New-ScheduledTaskTrigger -AtLogOn -User $User;$Principal=New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited;$Settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force|Out-Null;Start-ScheduledTask -TaskName $Task;Start-Sleep -Seconds 5
}
Write-Host "";Write-Host "================================================================================" -ForegroundColor Green;Write-Host " HISTORICAL ACCELERATOR V2 COMPLETE" -ForegroundColor Green;Write-Host "================================================================================" -ForegroundColor Green;Write-Host "Verdict: $($D.state)";Write-Host "Selected before final: $($D.selected_family_before_final)";Write-Host "Candidate: $($D.candidate_family)";Write-Host "V1 decision: SEALED / UNCHANGED";Write-Host "Forward PAPER: $(if(Test-Path $Candidate){'RUNNING / HIDDEN'}else{'NOT STARTED - NO FINAL PASS'})";Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green;Write-Host "Report: $Report";Write-Host "Decision: $Decision";Write-Host "================================================================================" -ForegroundColor Green
