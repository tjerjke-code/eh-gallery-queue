# Creates Desktop shortcut for EH Gallery Queue with a thematic icon.
# Same pattern as WishAssistance / movie-catalog / WP.
#
# Run from repo root (or any checkout):
#   powershell -ExecutionPolicy Bypass -File tools\Install-DesktopShortcuts.ps1
# Optional:
#   -Root <path>   Force launch tree (default: this checkout)

param(
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"

$ScriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Checkout = Resolve-Path (Join-Path $ScriptRoot "..")

if ($Root) {
    $LaunchRoot = (Resolve-Path $Root).Path
} else {
    $LaunchRoot = $Checkout.Path
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$IconRoot = $Checkout.Path

$StartBat = Join-Path $LaunchRoot "Start.bat"
$ProdIco = Join-Path $IconRoot "assets\icons\eh-prod.ico"

foreach ($path in @($StartBat, $ProdIco)) {
    if (-not (Test-Path $path)) {
        throw "Missing required file: $path"
    }
}

$Wsh = New-Object -ComObject WScript.Shell

$LinkPath = Join-Path $Desktop "EH Gallery Queue.lnk"
$shortcut = $Wsh.CreateShortcut($LinkPath)
$shortcut.TargetPath = $StartBat
$shortcut.WorkingDirectory = $LaunchRoot
$shortcut.IconLocation = "$ProdIco,0"
$shortcut.Description = "EH Gallery Queue (pythonw via Start.bat)"
$shortcut.WindowStyle = 1
$shortcut.Save()
Write-Host "Created $LinkPath"

Write-Host ""
Write-Host "Desktop shortcut ready:"
Write-Host "  EH Gallery Queue  -> Start.bat (pythonw preferred)"
Write-Host "Launch tree: $LaunchRoot"
Write-Host "Do not copy Start.bat alone to the Desktop - use this .lnk."
