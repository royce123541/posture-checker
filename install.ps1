<#
.SYNOPSIS
    Install Hunch (the posture checker) for the current user (no admin rights needed).

.DESCRIPTION
    Run from the repository folder:  .\install.ps1
    1. uv sync                          (dependencies into .venv)
    2. fetch the pose model             (checksum-verified)
    3. register the toast AUMID         (HKCU\Software\Classes\AppUserModelId\PostureChecker.App)
       and the Startup-folder shortcut "Hunch.lnk"
       -> "<repo>\.venv\Scripts\hunch.exe" (minimised, no arguments)
       Both go through `python -m posture_checker.install register`, the same
       code the Settings window uses. An old "Posture Checker.lnk" and the old
       "PostureChecker" Run value are removed.
    4. start the app and report whether it is still running after 5 seconds.
    Running it again is safe: the shortcut is overwritten (one shortcut) and the
    app's single-instance lock makes a second copy exit at once.

.PARAMETER NoAutostart
    Do not start the app at sign-in (removes the shortcut if present).
#>
param(
    [switch]$NoAutostart
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "1/4 Installing dependencies (uv sync)..."
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$hunch = Join-Path $PSScriptRoot ".venv\Scripts\hunch.exe"

Write-Host "2/4 Fetching the pose model..."
& $python -m posture_checker fetch-model
if ($LASTEXITCODE -ne 0) { throw "fetch-model failed" }

Write-Host "3/4 Registering toast identity (PostureChecker.App) and autostart (Startup shortcut 'Hunch')..."
$registerArgs = @("-m", "posture_checker.install", "register")
if ($NoAutostart) { $registerArgs += "--no-autostart" }
& $python @registerArgs
if ($LASTEXITCODE -ne 0) { throw "register failed" }

. (Join-Path $PSScriptRoot "scripts\posture-checker-processes.ps1")

Write-Host "4/4 Starting the app..."
if (Get-PostureCheckerProcess -RepoRoot $PSScriptRoot -AppOnly) {
    Write-Host "Hunch is already running; leaving it as it is."
} else {
    Start-Process -FilePath $hunch -WorkingDirectory $PSScriptRoot
    # hunch.exe has no console, so an early crash would be invisible: check it survived.
    Start-Sleep -Seconds 5
    if (Get-PostureCheckerProcess -RepoRoot $PSScriptRoot -AppOnly) {
        Write-Host "Hunch is running. Look for its icon in the system tray."
    } else {
        $log = Join-Path $env:LOCALAPPDATA "PostureChecker\posture-checker.log"
        Write-Warning "Hunch exited within 5 seconds. See the log: $log"
        exit 1
    }
}
