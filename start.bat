@echo off
REM Double-click this file on Windows to start the agent.
REM It installs what it needs the first time, then opens your browser.

cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python is not installed yet.
  echo   Get it from https://www.python.org/downloads/ ^(tick "Add Python to PATH"^),
  echo   then double-click this file again.
  echo.
  pause
  exit /b 1
)

if not exist .venv (
  echo   First run - setting things up. This takes a minute or two...
  %PY% -m venv .venv || goto fail
)

call .venv\Scripts\activate.bat

python -c "import fastapi, anthropic" >nul 2>&1
if errorlevel 1 (
  echo   Installing components...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || goto fail
)

set PYTHONPATH=src
python -m companies_research

echo.
echo   The agent has stopped.
pause
exit /b 0

:fail
echo.
echo   Setup failed. Please send the messages above to whoever set this up.
pause
exit /b 1
