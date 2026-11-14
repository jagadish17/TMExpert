@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required to run TMExpert.
  pause
  exit /b 1
)

rem Shares TMExpert with computers on this office network or VPN.
rem It refuses to start while any account still uses the default password.
rem The server window shows the "Team link" to send to your team.
set TMEXPERT_HOST=0.0.0.0
start "TMExpert Server (shared on the network)" /D "%~dp0" cmd.exe /k "python -u server.py"
timeout /t 3 /nobreak >nul
start "" "http://localhost:8787"

echo TMExpert is starting. Send your team the "Team link" shown in the server window.
echo Keep the server window open while people use TMExpert.
endlocal
