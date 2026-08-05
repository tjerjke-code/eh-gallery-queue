@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0eh_gallery_queue.py"
if not exist "%SCRIPT%" (
  echo Could not find eh_gallery_queue.py next to this file:
  echo   %SCRIPT%
  echo.
  echo Do not copy Start.bat alone. Use Create-Desktop-Shortcut.bat
  echo to put a shortcut on your Desktop, or run Start.bat from the
  echo project folder.
  pause
  exit /b 1
)

where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" pythonw "%SCRIPT%"
  exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" python "%SCRIPT%"
  exit /b 0
)

echo Python was not found on PATH.
echo Install Python and tick "Add python.exe to PATH", then try again.
pause
exit /b 1
