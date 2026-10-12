@echo off
setlocal
title TV_Service - install Windows service

rem ===========================================================================
rem  Installs the TV_Service MCP server as a Windows service that starts at
rem  Self-elevates: you will get a UAC prompt.
rem
rem  What it runs:
rem    python D:\TV_Service\heatmap_mcp.py --http --port 8099
rem
rem  Starts at boot as Automatic (Delayed Start), so it waits for the rest of
rem  the boot to settle instead of competing with it.
rem
rem  Safe to re-run: an existing tv_service is removed and reinstalled.
rem ===========================================================================

rem --- administrator check, self-elevate once -------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "SVC=tv_service"
set "NSSM=C:\Users\AndrewBot\AppData\Local\Microsoft\WinGet\Links\nssm.exe"
set "PY=C:\Users\AndrewBot\AppData\Local\Programs\Python\Python312\python.exe"
set "APP=D:\TV_Service\heatmap_mcp.py"
set "APPDIR=D:\TV_Service"
set "LOGDIR=D:\TV_Service\logs"
set "PORT=8099"

echo ============================================================
echo   Installing Windows service: %SVC%
echo ============================================================
echo.

rem --- preflight: every path must exist -------------------------------------
set "MISSING=0"
if not exist "%NSSM%"   ( echo ERROR: nssm not found at "%NSSM%" & set "MISSING=1" )
if not exist "%PY%"     ( echo ERROR: python not found at "%PY%" & set "MISSING=1" )
if not exist "%APP%"    ( echo ERROR: server not found at "%APP%" & set "MISSING=1" )
if "%MISSING%"=="1" ( echo. & pause & exit /b 1 )
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

rem --- remove any previous service so this is repeatable --------------------
sc query "%SVC%" >nul 2>&1
if %errorlevel% equ 0 (
    echo Removing the existing %SVC% service...
    "%NSSM%" stop "%SVC%" >nul 2>&1
    "%NSSM%" remove "%SVC%" confirm >nul 2>&1
    timeout /t 2 /nobreak >nul
)

echo Installing...
rem The paths above contain no spaces, so no nested quoting is needed.
"%NSSM%" install "%SVC%" "%PY%"
if errorlevel 1 ( echo ERROR: nssm install failed & echo. & pause & exit /b 1 )

"%NSSM%" set "%SVC%" AppParameters "%APP% --http --port %PORT%"
"%NSSM%" set "%SVC%" AppDirectory "%APPDIR%"
"%NSSM%" set "%SVC%" DisplayName "TV_Service (TradingView heat-map MCP)"
"%NSSM%" set "%SVC%" Description "Model Context Protocol server for TradingView heat-map analysis. Serves Streamable HTTP on http://127.0.0.1:%PORT%/mcp and is part of the TV_Service project."
"%NSSM%" set "%SVC%" Start SERVICE_AUTO_START
"%NSSM%" set "%SVC%" AppStdout "%LOGDIR%\service-stdout.log"
"%NSSM%" set "%SVC%" AppStderr "%LOGDIR%\service-stderr.log"
"%NSSM%" set "%SVC%" AppRotateFiles 1
"%NSSM%" set "%SVC%" AppRotateOnline 1
"%NSSM%" set "%SVC%" AppRotateBytes 1048576
"%NSSM%" set "%SVC%" AppExit Default Restart
"%NSSM%" set "%SVC%" AppRestartDelay 5000
"%NSSM%" set "%SVC%" AppStopMethodConsole 5000

echo.
echo Starting...
"%NSSM%" start "%SVC%"
timeout /t 4 /nobreak >nul
"%NSSM%" status "%SVC%"

rem ----------------------------------------------------------------------
rem  The delayed flag is applied LAST, after every nssm set call. nssm
rem  rewrites the service configuration when it sets a parameter, which
rem  clears DelayedAutostart. Applied earlier it is silently lost.
rem ----------------------------------------------------------------------
echo Flagging the start as delayed...
sc config "%SVC%" start= delayed-auto >nul
if errorlevel 1 echo   WARNING: sc config failed - the service stays plain Automatic.

echo Start type as Windows sees it:
sc qc "%SVC%" | findstr /i "START_TYPE"
powershell -NoProfile -Command "$v=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\%SVC%' -ErrorAction SilentlyContinue).DelayedAutostart; if ($v -eq 1) { Write-Host '  delayed flag: SET (1)' } else { Write-Host '  delayed flag: NOT SET - value is' $v }"

echo.
echo Verifying the endpoint...
powershell -NoProfile -Command "$b='{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"install-check\",\"version\":\"1\"}}}'; try { $r=Invoke-WebRequest 'http://127.0.0.1:%PORT%/mcp' -Method POST -Body $b -ContentType 'application/json' -UseBasicParsing -TimeoutSec 15; Write-Host ('  OK  HTTP ' + $r.StatusCode); Write-Host ('  ' + $r.Content.Substring(0,[Math]::Min(150,$r.Content.Length))) } catch { Write-Host ('  endpoint not answering yet: ' + $_.Exception.Message); Write-Host '  check the log below' }"

echo.
echo --- service-stderr.log (last 8 lines) ---
powershell -NoProfile -Command "if (Test-Path '%LOGDIR%\service-stderr.log') { Get-Content '%LOGDIR%\service-stderr.log' -Tail 8 } else { Write-Host '  (no log yet)' }"

echo.
echo ============================================================
echo   Done.  '%SVC%' is installed as Automatic (Delayed Start).
echo     endpoint : http://127.0.0.1:%PORT%/mcp
echo     logs     : %LOGDIR%\service-stderr.log
echo     status   : status-tv-service.bat
echo     remove   : uninstall-tv-service.bat
echo.
echo   If it starts then stops immediately, the account cannot read
echo   your Python install. Fix:  nssm edit %SVC%  -^> Log on tab
echo   -^> This account -^> .\AndrewBot
echo ============================================================
echo.
pause
