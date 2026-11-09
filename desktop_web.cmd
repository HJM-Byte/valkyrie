@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem Desktop shell for the exact sihai web UI.
rem This is the 1:1 option: the window renders the same sihai.html as 8767,
rem not a Tk reimplementation. The native desktop (desktop.cmd) still has
rem voice input/read-aloud and the response gate.

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PYW%" set "PYW="
if not exist "%PY%" set "PY=python"

set "PORT=8767"
set "URL=http://127.0.0.1:%PORT%/sihai.html"

rem Start the local sihai server only when it is not already listening.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { $r=Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -TimeoutSec 2; if($r.StatusCode -eq 200){ exit 0 } } catch {}; exit 1"
if errorlevel 1 (
    if defined PYW (
        start "" "%PYW%" -m app.sihai --serve --port %PORT%
    ) else (
        start "" "%PY%" -m app.sihai --serve --port %PORT%
    )
)

rem Wait for the page; the first start can take a few seconds.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$u='%URL%'; for($i=0;$i -lt 60;$i++){ try { if((Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 2).StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
    echo.
    echo  识海服务没有起来，检查 8767 是否被占用。
    pause
    exit /b 1
)

set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
    rem Last resort: let the default browser open the same page.
    start "" "%URL%"
    exit /b 0
)

rem --app gives a borderless app window: no address bar, no tabs, no browser
rem chrome. Here the page is the window, so it is visually 1:1 with 8767.
rem A dedicated profile keeps this window out of the user's normal Edge session.
rem force-device-scale-factor=1 keeps the CSS viewport at 1380 so the desktop
rem layout stays above the 980px breakpoint on this 150% display.
set "EDGE_PROFILE=%LOCALAPPDATA%\Valkyrie\sihai-edge"
if not exist "%EDGE_PROFILE%" mkdir "%EDGE_PROFILE%" >nul 2>&1
start "" "%EDGE%" --app="%URL%" --user-data-dir="%EDGE_PROFILE%" --no-first-run --no-default-browser-check --force-device-scale-factor=1 --window-size=1380,880 --window-position=60,40
exit /b 0
