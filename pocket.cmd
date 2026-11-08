@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo 瓦尔基里 · 水镜
echo 手机和电脑连同一个 Wi-Fi 时，用下面打印出来的局域网地址。
echo 想只看不写，追加 --readonly；想看示例，追加 --demo；想开口，双击 talk.cmd。
echo.

"%PY%" -m app.pocket_server --lan %*
if errorlevel 1 pause
endlocal
