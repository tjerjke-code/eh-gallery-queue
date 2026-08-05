@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" pythonw "%~dp0eh_gallery_queue.py"
  exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  start "" python "%~dp0eh_gallery_queue.py"
  exit /b 0
)

echo Python was not found on PATH.
echo Install Python and tick "Add python.exe to PATH", then try again.
pause
exit /b 1
