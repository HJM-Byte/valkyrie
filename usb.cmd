@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "ADB="
where adb >nul 2>nul && set "ADB=adb"
if not defined ADB if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" set "ADB=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
if not defined ADB if exist "%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe" set "ADB=%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe"

for /f "usebackq delims=" %%t in ("data\pocket_token.txt") do set "TOKEN=%%t"

echo.
if defined ADB (
    "%ADB%" reverse tcp:8766 tcp:8766
    if errorlevel 1 (
        echo 反向转发失败。确认手机已用 USB 连上、开了 USB 调试、并在手机上允许这台电脑。
        pause
        exit /b 1
    )
    echo 有线已经通了。
    echo 手机浏览器打开：http://127.0.0.1:8766
    if defined TOKEN echo 配对码：%TOKEN%
    echo 如果服务还没开，先在另一个窗口运行 pocket.cmd（只看）或 talk.cmd（对话）。
) else (
    echo 这台电脑没找到 adb。
    echo.
    echo 两条不用 adb 的有线路：
    echo   1. Android 打开「USB 网络共享」，电脑运行 pocket.cmd --lan 或 talk.cmd；
    echo      手机打开启动时打印的局域网地址（USB 共享网段，通常是 192.168.42.x）。
    echo   2. 装 Android SDK Platform-Tools 后，再运行一次 usb.cmd，走 adb 反向转发。
    echo.
    echo 无线更简单：同一 Wi-Fi，运行 pocket.cmd --lan，手机打开打印出来的地址。
)
echo.
pause
endlocal
