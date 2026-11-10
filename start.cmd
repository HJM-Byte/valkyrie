@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Valkyrie - PC launcher

:menu
cls
echo.
echo   Valkyrie - PC launcher
echo   ---------------------------------------------
echo   1  Web (default)     web.cmd (chat + sihai, 8766)
echo   2  Phone chat        talk.cmd (8766, same Wi-Fi)
echo   3  Voice pipeline    run.cmd (mic + camera + board)
echo   4  Text entry        chat.cmd (no microphone needed)
echo   5  Phone read-only   pocket.cmd (look, do not wake her)
echo   6  Sihai standalone  sihai.cmd (8767, read-only star map)
echo   Q  Quit
echo.
echo   Response gate: enforced, social silence: on.
echo   Add --response-gate off to a .cmd to restore old behavior.
echo.
choice /c 123456Q /n /m "Choose: "
if errorlevel 7 goto end
if errorlevel 6 goto sihai
if errorlevel 5 goto pocket
if errorlevel 4 goto chat
if errorlevel 3 goto voice
if errorlevel 2 goto phone
if errorlevel 1 goto web

:phone
call "%~dp0talk.cmd"
goto menu

:voice
call "%~dp0run.cmd"
goto menu

:chat
call "%~dp0chat.cmd"
goto menu

:pocket
call "%~dp0pocket.cmd"
goto menu

:web
call "%~dp0web.cmd"
goto menu

:sihai
call "%~dp0sihai.cmd"
goto menu

:end
endlocal
