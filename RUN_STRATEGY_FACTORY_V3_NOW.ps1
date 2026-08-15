#requires -Version 5.1
<# One-command bounded Strategy Factory V3. PAPER/RESEARCH only. #>
$ErrorActionPreference="Stop"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root="C:\TradingCore_Strategy_Factory_V3"
function Fail([string]$Text){Write-Host "";Write-Host "STRATEGY FACTORY V3 STOPPED SAFELY" -ForegroundColor Red;Write-Host $Text -ForegroundColor Yellow;Write-Host "Existing TradingCore PAPER services continue. LIVE / real orders DISABLED." -ForegroundColor Green;exit 1}
Write-Host "";Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host " TRADINGCORE STRATEGY FACTORY V3 - BOUNDED SEARCH" -ForegroundColor Cyan;Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host "Early Bybit development -> frozen winner -> later OKX final holdout.";Write-Host "No terminal cleanup. No API keys. No real orders."
if(-not(Test-Path $Repo)){Fail "Repo missing"};if(-not(Test-Path $Py)){Fail "Python missing"};New-Item -ItemType Directory -Force -Path $Root|Out-Null
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true"
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY")|%{Remove-Item "Env:$_" -ErrorAction SilentlyContinue}
Push-Location $Repo
try{
 Write-Host "Compile + safety self-test..." -ForegroundColor Cyan
 & $Py -m py_compile .\strategy_factory_v3_protocol.py .\strategy_factory_v3.py .\strategy_factory_v3_selftest.py
 if($LASTEXITCODE -ne 0){Fail "Python compile failed"}
 & $Py .\strategy_factory_v3_selftest.py
 if($LASTEXITCODE -ne 0){Fail "V3 self-test failed"}
 Write-Host "";Write-Host "Running bounded development search and sealed final test..." -ForegroundColor Cyan
 & $Py .\strategy_factory_v3.py --state-dir $Root
 if($LASTEXITCODE -ne 0){Fail "V3 engine returned non-zero"}
}finally{Pop-Location}
$Decision="$Root\DECISION_LOCK.json";if(-not(Test-Path $Decision)){Fail "Decision lock not created"};$D=Get-Content $Decision -Raw|ConvertFrom-Json
Write-Host "";Write-Host "================================================================================" -ForegroundColor Green;Write-Host " STRATEGY FACTORY V3 COMPLETE" -ForegroundColor Green;Write-Host "================================================================================" -ForegroundColor Green;Write-Host "Verdict: $($D.state)";Write-Host "Candidate: $($D.candidate)";Write-Host "BTC 1H existing Forward Shadow: LEFT RUNNING";Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green;Write-Host "Decision: $Decision";Write-Host "================================================================================" -ForegroundColor Green
