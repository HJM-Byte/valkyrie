@echo off
rem Text entry. No microphone needed. Type /state for status; empty line exits.
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo  Valkyrie - text entry
echo  ---------------------------------------------
rem 2026-11-07: text entry also uses the response gate.
"%PY%" -m app.chat --response-gate enforced --social-silence on %*
if errorlevel 1 pause
