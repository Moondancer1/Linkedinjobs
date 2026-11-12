@echo off
rem One-click launcher. First run on a new machine sets everything up by itself, then starts the bot.
rem Control the bot from the Telegram chat: /go, /stop, /status.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem A venv copied from another PC or folder is broken: remove it, it is rebuilt below.
if not exist ".venv\Scripts\python.exe" goto check_ready
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 rmdir /s /q ".venv"

:check_ready
if exist ".venv\.ready" goto run

rem First run on this machine: find Python, create the venv, install dependencies and the browser.
set "PY="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto make_venv
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto make_venv
echo Python 3.10+ was not found.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this file again.
pause
exit /b 1

:make_venv
echo First run on this machine: setting up. This takes a few minutes, once.
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto fail
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail
echo ready> ".venv\.ready"

:run
".venv\Scripts\python.exe" main.py
echo.
echo Bot stopped.
pause
exit /b 0

:fail
echo.
echo Setup failed, see the messages above.
pause
exit /b 1
