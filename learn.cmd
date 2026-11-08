@echo off
rem 学习：渠道 / 夜间窗口 / 待审区 / 好奇心。不带参数打印总览。
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if "%~1"=="" (
  "%PY%" -m app.learning --stats
) else (
  "%PY%" -m app.learning %*
)
if errorlevel 1 pause
