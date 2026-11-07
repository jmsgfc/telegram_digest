@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto check_dependencies

where py >nul 2>nul
if errorlevel 1 goto create_with_python

py -3.10 -m venv .venv
if errorlevel 1 py -m venv .venv
if errorlevel 1 goto setup_failed
goto check_dependencies

:create_with_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python -m venv .venv
if errorlevel 1 goto setup_failed

:check_dependencies
"%PYTHON_EXE%" -c "import fastapi, telethon, uvicorn" >nul 2>nul
if not errorlevel 1 goto start_web

"%PYTHON_EXE%" -m pip install -e .
if errorlevel 1 goto setup_failed

:start_web
powershell -NoProfile -Command "$client=[Net.Sockets.TcpClient]::new(); try { $client.Connect('127.0.0.1', 8765); $client.Dispose(); exit 1 } catch { $client.Dispose(); exit 0 }"
if errorlevel 1 goto port_in_use

set "PYTHONPATH=%~dp0src"
start "Telegram Digest" "%PYTHON_EXE%" -m uvicorn telegram_digest.web:app --app-dir src --host 127.0.0.1 --port 8765
powershell -NoProfile -Command "$url='http://127.0.0.1:8765'; for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -UseBasicParsing -Uri ($url + '/api/health') -TimeoutSec 1 | Out-Null; Start-Process $url; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"
if errorlevel 1 goto start_failed
exit /b 0

:start_failed
echo The web service did not become ready. Check the Telegram Digest window for details.
pause
exit /b 1

:port_in_use
echo Port 8765 is already in use. Stop the Docker version or the other local instance first.
pause
exit /b 1

:missing_python
echo Python 3.10 or later is required.
echo Download: https://www.python.org/downloads/
pause
exit /b 1

:setup_failed
echo Setup failed. Check the network connection and Python installation.
pause
exit /b 1
