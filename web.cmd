@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem Open the local web UI, starting the existing watchdog once if needed.
rem Long-term autostart is installed by tools\install_web_persistence.ps1.
set "ROBOT=%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "TOKEN="
if exist "%ROBOT%data\pocket_token.txt" set /p TOKEN=<"%ROBOT%data\pocket_token.txt"
set "URL=http://127.0.0.1:8766/"
if defined TOKEN set "URL=%URL%?token=%TOKEN%"

echo.
echo Valkyrie - web UI (chat + sihai)
echo Local: %URL%
echo Starting the local service if it is not already up...
echo.

if exist "%ROBOT%tools\server_watchdog.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROBOT%tools\server_watchdog.ps1" >nul 2>&1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$u='http://127.0.0.1:8766/api/health'; for($i=0;$i -lt 40;$i++){ try { if((Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2).StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
    echo.
    echo The service did not come up. Check data\watchdog.log and data\pocket_server.err.log.
    pause
    exit /b 1
)

if /i "%~1"=="--no-open" exit /b 0
start "" "%URL%"
exit /b 0
