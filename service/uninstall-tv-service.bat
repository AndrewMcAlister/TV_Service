@echo off
setlocal
title TV_Service - uninstall Windows service

rem ===========================================================================
rem  Stops and removes the tv_service Windows service.
rem  Self-elevates: you will get a UAC prompt.
rem  Your project files in D:\TV_Service are NOT touched.
rem ===========================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "SVC=tv_service"
set "NSSM=C:\Users\AndrewBot\AppData\Local\Microsoft\WinGet\Links\nssm.exe"

echo ============================================================
echo   Removing Windows service: %SVC%
echo ============================================================
echo.

sc query "%SVC%" >nul 2>&1
if %errorlevel% neq 0 (
    echo Nothing to do: no service named %SVC% is installed.
    echo.
    pause
    exit /b 0
)

echo Stopping...
"%NSSM%" stop "%SVC%"
timeout /t 3 /nobreak >nul

echo Removing...
"%NSSM%" remove "%SVC%" confirm
timeout /t 2 /nobreak >nul

echo.
sc query "%SVC%" >nul 2>&1
if %errorlevel% equ 0 (
    echo WARNING: %SVC% still appears to be registered.
) else (
    echo Removed. No service named %SVC% remains.
)
echo.
echo Project files under D:\TV_Service were not touched.
echo.
pause
