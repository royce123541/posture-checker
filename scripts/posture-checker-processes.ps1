<#
    Find Posture Checker processes started from THIS checkout's virtual env.
    Dot-sourced by install.ps1 and uninstall.ps1.

    Matching is by executable path, not by command-line text, so it:
    - finds every way the app runs: "pythonw -m posture_checker app",
      "posture-checker app" (the console-script exe) and the "--gui calibrate" /
      "--gui settings" windows;
    - ignores other checkouts and unrelated Python programs.
    A uv venv's python.exe / pythonw.exe is a launcher that starts the base
    interpreter as a child, so children of matched processes are included too.
#>

function Get-PostureCheckerProcess {
    param(
        [Parameter(Mandatory)] [string]$RepoRoot,
        # Only the background app itself (not the calibrate/settings windows).
        [switch]$AppOnly
    )
    $scripts = Join-Path $RepoRoot ".venv\Scripts"
    $ours = @(
        (Join-Path $scripts "pythonw.exe"),
        (Join-Path $scripts "python.exe"),
        (Join-Path $scripts "posture-checker.exe")
    ) | ForEach-Object { $_.ToLowerInvariant() }

    $all = @(Get-CimInstance Win32_Process)
    $roots = @($all | Where-Object {
        $_.ExecutablePath -and ($ours -contains $_.ExecutablePath.ToLowerInvariant()) -and
        $_.CommandLine -match "posture[-_]checker"
    })
    if ($AppOnly) {
        $roots = @($roots | Where-Object { $_.CommandLine -match "\sapp\s*$" })
    }
    $rootIds = @($roots | ForEach-Object { $_.ProcessId })
    $children = @($all | Where-Object { $rootIds -contains $_.ParentProcessId })
    return @($roots + $children)
}
