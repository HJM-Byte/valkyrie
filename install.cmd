@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Valkyrie Setup

echo.
echo   Valkyrie - Windows installer
echo   ---------------------------------------------
echo   First install creates .venv and installs dependencies.
echo   For microphone / camera / local voice, run: install.cmd -Voice
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [x] Installer failed, exit code %RC%. See install.log
) else (
    echo [ok] Done.
)
echo.
pause
exit /b %RC%
