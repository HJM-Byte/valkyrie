@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [body] venv python not found: %PY%
  exit /b 2
)
set PYTHONUTF8=1
rem 2026-11-03: the board may connect, but it is READ-ONLY until the owner
rem passes the full inspection. Do not set BODY_ALLOW_WRITES=1 on your own.
rem BODY_LIVE=1 additionally selects the real memory.db -- only after approval.
set "LIVE="
set "NOWRITE=--no-write"
if /i "%BODY_ALLOW_WRITES%"=="1" (
  set "NOWRITE="
  if /i "%BODY_LIVE%"=="1" set "LIVE=--live-memory"
)
echo [body] OTA  http://<PC-IP>:8768/xiaozhi/ota/
echo [body] WS   ws://<PC-IP>:8768/xiaozhi/v1/
echo [body] health http://127.0.0.1:8768/health
if defined NOWRITE (
  echo [body] READ-ONLY: board can connect; memory/mind/flow/learning writes are blocked
) else (
  echo [body] WRITE MODE: BODY_ALLOW_WRITES=1 was set explicitly
)
"%PY%" -m app.xiaozhi_server --host 0.0.0.0 --port 8768 --log "data\xiaozhi_server.log" --chat %NOWRITE% %LIVE% %*
