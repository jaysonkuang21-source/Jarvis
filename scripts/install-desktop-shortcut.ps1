#Requires -Version 5.1
<#
.SYNOPSIS
  Create a Desktop (and optional Start Menu) shortcut that launches Jarvis web UI.

.DESCRIPTION
  Writes %USERPROFILE%\Desktop\Jarvis.lnk targeting start-web.ps1 with the repo
  root as the working directory. Uses scripts/assets/jarvis.ico when present;
  otherwise falls back to powershell.exe's icon and prints a note.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StartScript = Join-Path $PSScriptRoot "start-web.ps1"
$IconPath = Join-Path $PSScriptRoot "assets\jarvis.ico"

if (-not (Test-Path $StartScript)) {
    throw "Missing launcher script: $StartScript"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($Desktop) -or -not (Test-Path $Desktop)) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}
$ShortcutPath = Join-Path $Desktop "Jarvis.lnk"

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.WindowStyle = 1
$Shortcut.Description = "Start Jarvis web UI (FastAPI + Vite)"

$usedFallbackIcon = $false
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
} else {
    # Fallback: PowerShell icon until scripts/assets/jarvis.ico is present.
    # Regenerate the violet squircle via scripts/make_icons.py and copy icon.ico,
    # or place any .ico at scripts/assets/jarvis.ico and re-run this installer.
    $Shortcut.IconLocation = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe,0"
    $usedFallbackIcon = $true
}

$Shortcut.Save()
Write-Host "Created Desktop shortcut: $ShortcutPath"

# Optional Start Menu entry (same target).
$StartMenuPrograms = [Environment]::GetFolderPath("Programs")
if (-not [string]::IsNullOrWhiteSpace($StartMenuPrograms) -and (Test-Path $StartMenuPrograms)) {
    $StartMenuShortcut = Join-Path $StartMenuPrograms "Jarvis.lnk"
    Copy-Item -Path $ShortcutPath -Destination $StartMenuShortcut -Force
    Write-Host "Created Start Menu shortcut: $StartMenuShortcut"
}

if ($usedFallbackIcon) {
    Write-Host ""
    Write-Host "Note: scripts/assets/jarvis.ico was missing; used the PowerShell icon."
    Write-Host "Add an ICO at that path and re-run this script to update the shortcut icon."
}

Write-Host ""
Write-Host "Double-click Desktop 'Jarvis' to start the web UI."