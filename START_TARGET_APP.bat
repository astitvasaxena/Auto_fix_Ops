@echo off
rem Double-click to open browser access to the AutoFixOps Kubernetes target app on http://localhost:30081
rem (a kubectl port-forward kept alive by scripts\start-target-access.ps1). Only reads from the cluster.
rem Extra arguments are passed to the script, e.g.  START_TARGET_APP.bat -CheckOnly
setlocal
title AutoFixOps target app - browser access (Ctrl+C to stop)
cd /d "%~dp0"

set "SCRIPT=%~dp0scripts\start-target-access.ps1"
if not exist "%SCRIPT%" (
    echo ERROR: %SCRIPT% was not found. Run this file from the AutoFixOps project folder.
    goto :done_error
)
rem Full path: powershell.exe is not always on PATH.
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" (
    echo ERROR: Windows PowerShell was not found at %PS%.
    goto :done_error
)

"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Browser access stopped.
) else (
    echo Browser access did not start or stopped with an error ^(exit code %RC%^). See the messages above.
)
pause
exit /b %RC%

:done_error
echo.
pause
exit /b 1
