<#
.SYNOPSIS
    Uninstall Hunch (the posture checker) for the current user.

.DESCRIPTION
    Stops the running app, removes the Startup-folder shortcut "Hunch.lnk"
    (and an old "Posture Checker.lnk")
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

Write-Host "Stopping Hunch if it is running..."
# The app plus any calibrate / settings windows, from this checkout only.
. (Join-Path $PSScriptRoot "scripts\posture-checker-processes.ps1")
Get-PostureCheckerProcess -RepoRoot $PSScriptRoot |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Normally the Python code path (posture_checker.install), the same code the
# installer and Settings use.
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $python) {
    $unregisterArgs = @("-m", "posture_checker.install", "unregister")
    if ($RemoveData) { $unregisterArgs += "--remove-data" }
    & $python @unregisterArgs
} else {
    # No venv (never installed, or deleted): remove the same things directly,
    # without creating a venv.
    $startup = [Environment]::GetFolderPath("Startup")
    foreach ($name in @("Hunch.lnk", "Posture Checker.lnk")) {
        Remove-Item -Path (Join-Path $startup $name) -Force -ErrorAction SilentlyContinue
    }
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "PostureChecker" -ErrorAction SilentlyContinue
    Remove-Item -Path "HKCU:\Software\Classes\AppUserModelId\PostureChecker.App" `
        -Recurse -ErrorAction SilentlyContinue
    if ($RemoveData) {
        Remove-Item -Path (Join-Path $env:LOCALAPPDATA "PostureChecker") `
            -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($RemoveData) {
    Write-Host "Hunch removed, including its data."
} else {
    Write-Host "Hunch removed. Your data is kept in $env:LOCALAPPDATA\PostureChecker"
}
exit 0
