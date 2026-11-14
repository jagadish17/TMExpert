@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required to run TMExpert.
  echo Install Python and then run this launcher again.
  pause
  exit /b 1
)

rem Keep this separate console visible. If startup fails, it shows the error
rem instead of silently closing before the browser can connect.
start "TMExpert Server" /D "%~dp0" cmd.exe /k "python -u server.py"
timeout /t 2 /nobreak >nul
start "" "http://localhost:8787"

echo TMExpert has been started at http://localhost:8787
echo Keep the TMExpert Server window running while using the app.
endlocal
