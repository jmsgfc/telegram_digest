@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m telegram_digest.cli menu
exit /b %errorlevel%

:missing
echo Runtime is not installed. Please read README.md first.
pause
exit /b 1
