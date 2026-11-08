@echo off
rem 休眠。不带参数看状态；带 hibernate 封存此刻让她睡。
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if /i "%~1"=="hibernate" (
  "%PY%" -m app.sleep --hibernate
) else (
  "%PY%" -m app.sleep --status
)
if errorlevel 1 pause
