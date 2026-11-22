<#
.SYNOPSIS
    Uninstall Posture Checker for the current user.

.DESCRIPTION
    Stops the running app, removes the HKCU Run value "PostureChecker" and the
    toast identity key HKCU\Software\Classes\AppUserModelId\PostureChecker.App.
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

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $python) {
    $unregisterArgs = @("-m", "posture_checker.install", "unregister")
    if ($RemoveData) { $unregisterArgs += "--remove-data" }
    & $python @unregisterArgs
} else {
    # No venv (never installed, or deleted): do the same with PowerShell.
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
    Write-Host "Posture Checker removed, including its data."
} else {
    Write-Host "Posture Checker removed. Your data is kept in $env:LOCALAPPDATA\PostureChecker"
}
exit 0
