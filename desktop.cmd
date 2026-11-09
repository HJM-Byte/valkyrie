@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Optional exact web UI shell: desktop.cmd --web
if /i "%~1"=="--web" (
    call "%~dp0desktop_web.cmd"
    exit /b %errorlevel%
)

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PY=%~dp0.venv\Scripts\python.exe"
rem 2026-11-07: response gate is enforced by default.
rem To go back to old behavior, append --response-gate off.
rem To disable social silence only, append --social-silence off.
if exist "%PYW%" (
    start "" "%PYW%" -m app.desktop --response-gate enforced --social-silence on %*
    exit /b 0
)
if exist "%PY%" (
    "%PY%" -m app.desktop --response-gate enforced --social-silence on %*
    if errorlevel 1 pause
    exit /b %errorlevel%
)
python -m app.desktop --response-gate enforced --social-silence on %*
if errorlevel 1 pause
endlocal
