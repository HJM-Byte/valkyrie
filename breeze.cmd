@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if "%BREEZE_API_KEY%"=="" (
    set /p BREEZE_API_KEY=粘贴 BreezeBlue Key（只在本进程，不落盘）: 
)
if "%BREEZE_API_KEY%"=="" (
    echo 未输入 BREEZE_API_KEY，退出。
    endlocal
    exit /b 1
)
call "%~dp0desktop.cmd" %*
endlocal
