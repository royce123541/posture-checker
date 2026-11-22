<#
.SYNOPSIS
    Uninstall Posture Checker for the current user.

.DESCRIPTION
    Stops the running app, removes the Startup-folder shortcut "Posture Checker.lnk"
    (and any old HKCU Run value "PostureChecker") and the toast identity key HKCU\Software\Classes\AppUserModelId\PostureChecker.App.
    Your data (database, calibrations, settings, log) in
    %LOCALAPPDATA%\PostureChecker is kept unless -RemoveData is given.
    Safe to run twice, or without a prior install.

.PARAMETER RemoveData
    Also delete %LOCALAPPDATA%\PostureChecker.
#>
param(
    [switch]$RemoveData
)

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

Write-Host "Stopping Posture Checker if it is running..."
# The app plus any calibrate / settings windows, from this checkout only.
. (Join-Path $PSScriptRoot "scripts\posture-checker-processes.ps1")
Get-PostureCheckerProcess -RepoRoot $PSScriptRoot |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Always the Python code path (posture_checker.install), never a copy of it here.
$unregisterArgs = @("-m", "posture_checker.install", "unregister")
if ($RemoveData) { $unregisterArgs += "--remove-data" }
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $python) {
    & $python @unregisterArgs
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    # No venv (never installed, or deleted): let uv build one to run the same code.
    uv run python @unregisterArgs
} else {
    Write-Warning "No .venv and no uv found: could not remove autostart or the toast identity."
    exit 1
}

if ($RemoveData) {
    Write-Host "Posture Checker removed, including its data."
} else {
    Write-Host "Posture Checker removed. Your data is kept in $env:LOCALAPPDATA\PostureChecker"
}
exit 0
