@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ from https://www.python.org/downloads/
    echo and make sure "Add python.exe to PATH" is checked during setup.
    pause
    exit /b 1
)

python main.py %*
if errorlevel 1 (
    echo.
    echo InkDoc exited with an error.
    pause
)
endlocal
