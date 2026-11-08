@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set "TMP=%~dp0data\_tmp"
set "TEMP=%~dp0data\_tmp"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv not found at %PY%
    echo Run bootstrap.ps1 first.
    pause
    exit /b 1
)

rem 2026-11-07: voice pipeline also uses the response gate.
"%PY%" -m app --response-gate enforced --social-silence on %*
endlocal
