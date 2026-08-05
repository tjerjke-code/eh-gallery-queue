@echo off
setlocal
cd /d "%~dp0"

set "TARGET=%~dp0Start.bat"
if not exist "%TARGET%" (
  echo Start.bat not found next to this file.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$lnkPath = Join-Path $desktop 'EH Gallery Queue.lnk';" ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $ws.CreateShortcut($lnkPath);" ^
  "$lnk.TargetPath = '%TARGET%';" ^
  "$lnk.WorkingDirectory = '%~dp0';" ^
  "$lnk.WindowStyle = 7;" ^
  "$lnk.Description = 'EH Gallery Queue';" ^
  "$lnk.Save();" ^
  "Write-Host \"Created: $lnkPath\""

if errorlevel 1 (
  echo Failed to create desktop shortcut.
  pause
  exit /b 1
)

echo.
echo Shortcut created on Desktop: EH Gallery Queue
echo You can move that shortcut anywhere; it still points here.
pause
