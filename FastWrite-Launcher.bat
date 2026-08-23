@echo off
title FastWrite Launcher
cd /d "%~dp0"

set FW_PORT=8964

rem ---- Is the port already taken, and if so by us or by something else? ----
rem   exit 0 = FastWrite already running here -> just open the browser
rem   exit 1 = some other program owns the port -> tell the user, do not start
rem   exit 2 = port is free -> proceed to start the backend normally
powershell -NoProfile -Command "$p=%FW_PORT%; try { $t=New-Object System.Net.Sockets.TcpClient; $iar=$t.BeginConnect('127.0.0.1',$p,$null,$null); $ok=$iar.AsyncWaitHandle.WaitOne(300); if (-not $ok) { $t.Close(); exit 2 }; $t.EndConnect($iar); $t.Close() } catch { exit 2 }; try { $r=Invoke-RestMethod -Uri ('http://127.0.0.1:' + $p + '/health') -TimeoutSec 2; if ($r.engine -eq 'funasr') { exit 0 } else { exit 1 } } catch { exit 1 }"
if errorlevel 2 goto port_free
if errorlevel 1 goto port_conflict
goto already_running

:already_running
echo [FastWrite] Already running at http://localhost:%FW_PORT%/ - opening browser.
start "" "http://localhost:%FW_PORT%/"
goto end

:port_conflict
echo [FastWrite] Port %FW_PORT% is already used by another program (not FastWrite).
echo Close whatever is using port %FW_PORT%, then run this launcher again.
pause
goto end

:port_free

rem ---- Preferred: local Python engine (FunASR, audio stays on this PC) ----
rem   .deps_ok is only written after a successful pip install, so a venv left
rem   behind by a FAILED first-run install is not mistaken for a working one.
if exist "server\.venv\Scripts\python.exe" if exist "server\.venv\.deps_ok" goto run_backend

python --version >nul 2>&1
if errorlevel 1 goto fallback

echo [FastWrite] First run: creating venv and installing deps (incl. torch, a few minutes)...
if not exist "server\.venv\Scripts\python.exe" python -m venv server\.venv
if errorlevel 1 goto fallback
server\.venv\Scripts\python.exe -m pip install --upgrade pip -q
server\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
if errorlevel 1 (
  echo [FastWrite] Dependency install failed. Falling back to browser recognition.
  goto fallback
)
type nul > "server\.venv\.deps_ok"

:run_backend
echo [FastWrite] Starting local recognition engine (first model load may take 10-60s, browser opens automatically)...
server\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port %FW_PORT% --app-dir server
goto end

rem ---- Fallback: no Python = static server; frontend degrades to Web Speech API ----
:fallback
echo [FastWrite] No usable Python found. Using browser recognition (fallback mode).
echo Ensure FastWrite-Server.ps1 is in the same folder, then press any key...
pause
if exist "%~dp0FastWrite-Server.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0FastWrite-Server.ps1"
) else (
  echo [FastWrite] FastWrite-Server.ps1 not found. Please start it manually.
  pause
)

:end
pause
