@echo off
rem Build and run InkDoc as an .exe from the current local working tree.
rem Double-click this file, or pass through any scripts\build_local_exe.ps1 flag:
rem   build-local.bat -OneFile
rem   build-local.bat -RunOnly
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_local_exe.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo Local build/run failed with exit code %EXITCODE%.
    pause
)
endlocal & exit /b %EXITCODE%
