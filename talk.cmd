@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo Valkyrie - phone chat service (8766)
echo Same Wi-Fi, or run usb.cmd after enabling USB debugging.
echo The first message wakes her and consumes the sleep record.
echo Default: response gate enforced, social silence on.
echo Add --response-gate off to restore the old behavior.
echo.
"%PY%" -m app.pocket_server --lan --chat --response-gate enforced --social-silence on %*
if errorlevel 1 pause
endlocal
