@echo off
setlocal
title TV_Service - status

rem ===========================================================================
rem  Shows whether the tv_service Windows service is running and answering.
rem  Does NOT need administrator rights.
rem ===========================================================================

set "SVC=tv_service"
set "NSSM=C:\Users\AndrewBot\AppData\Local\Microsoft\WinGet\Links\nssm.exe"
set "PORT=8099"
set "LOGDIR=D:\TV_Service\logs"

echo ============================================================
echo   TV_Service status
echo ============================================================
echo.

sc query "%SVC%" >nul 2>&1
if %errorlevel% neq 0 (
    echo Service '%SVC%' is NOT installed.
    echo   install it with: install-tv-service.bat  ^(as administrator^)
    echo.
    pause
    exit /b 1
)

echo --- service state ---
powershell -NoProfile -Command "$s = Get-Service -Name '%SVC%'; Write-Host ('  name   : ' + $s.Name); Write-Host ('  status : ' + $s.Status); Write-Host ('  start  : ' + $s.StartType)"
echo.
sc qc "%SVC%" | findstr /i "BINARY_PATH_NAME SERVICE_START_NAME"

echo.
echo --- listening on %PORT% ---
netstat -ano | findstr ":%PORT%" | findstr LISTENING

echo.
echo --- endpoint check ---
powershell -NoProfile -Command "$b='{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"status-check\",\"version\":\"1\"}}}'; try { $r=Invoke-WebRequest 'http://127.0.0.1:%PORT%/mcp' -Method POST -Body $b -ContentType 'application/json' -UseBasicParsing -TimeoutSec 10; Write-Host ('  OK  HTTP ' + $r.StatusCode + '  ' + $r.Content.Substring(0,[Math]::Min(140,$r.Content.Length))) } catch { Write-Host ('  NOT ANSWERING: ' + $_.Exception.Message) }"

echo.
echo --- service-stderr.log (last 12 lines) ---
powershell -NoProfile -Command "if (Test-Path '%LOGDIR%\service-stderr.log') { Get-Content '%LOGDIR%\service-stderr.log' -Tail 12 } else { Write-Host '  (no log yet)' }"

echo.
pause
