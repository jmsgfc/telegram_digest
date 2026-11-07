@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto missing
set "PYTHONPATH=%~dp0src"
start "" ".venv\Scripts\pythonw.exe" -m telegram_digest.gui
exit /b 0

:missing
echo Runtime is not installed. Please read README.md first.
pause
exit /b 1
