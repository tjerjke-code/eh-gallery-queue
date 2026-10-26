@echo off
REM Daily launcher: detached app (pythonw). Desktop shortcut:
REM   powershell -ExecutionPolicy Bypass -File tools\Install-DesktopShortcuts.ps1
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0eh_gallery_queue.py"
if not exist "%SCRIPT%" (
  echo Could not find eh_gallery_queue.py next to this file:
  echo   %SCRIPT%
  echo.
  echo Do not copy Start.bat alone. Install the Desktop .lnk via
  echo   tools\Install-DesktopShortcuts.ps1
  echo or run Start.bat from the project folder.
  pause
  exit /b 1
)

REM Prefer local venv (WishAssistance-style), then PATH.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%SCRIPT%"
  exit /b 0
)
if exist "%~dp0.venv\Scripts\python.exe" (
  start "" "%~dp0.venv\Scripts\python.exe" "%SCRIPT%"
  exit /b 0
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

echo Python was not found (.venv or PATH).
echo Create .venv or install Python with "Add python.exe to PATH", then try again.
pause
exit /b 1
