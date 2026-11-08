@echo off
rem 识海 · 记忆星图。默认读 data\memory.db；加 --demo 看示例数据。
rem 8767 是电脑端统一入口；重复启动只会打开已有页面。
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo  识海 · 记忆星图   http://127.0.0.1:8767/sihai.html
echo  ---------------------------------------------
"%PY%" -m app.sihai --serve --open --port 8767 %*
if errorlevel 1 pause
