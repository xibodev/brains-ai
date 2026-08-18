# install-windows.ps1 — register the brains supervisor as a Windows Scheduled Task.
#
# Run from any PowerShell session (no admin required for per-user task):
#   powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
#
# Honors $env:BRAINS_STATE_DIR and $env:PYTHON_BIN if set.
#
# Prereq: install the package first (recommended: pipx)
#   pipx install .
# This puts `brains` on PATH inside an isolated venv.
# If `brains` isn't on PATH, the task falls back to
# `<PYTHON_BIN> -m brains.control.supervisor`.

$ErrorActionPreference = "Stop"

$taskName = "BrainsService"
$installDir = $PSScriptRoot
if (-not $installDir) { $installDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$codeDir = Split-Path -Parent $installDir

$brainsCmd = (Get-Command brains -ErrorAction SilentlyContinue)
$python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }

# Build the cmd.exe wrapper that sets BRAINS_STATE_DIR (if provided) then
# launches the supervisor. cmd.exe is more reliable than direct invocation
# under Task Scheduler because pythonw.exe / Scripts shim resolution varies.
$argParts = @()
if ($env:BRAINS_STATE_DIR) {
    $argParts += "set BRAINS_STATE_DIR=$($env:BRAINS_STATE_DIR) &&"
}
if ($brainsCmd) {
    $argParts += "`"$($brainsCmd.Source)`" serve-all"
    Write-Host "Using installed entry point: $($brainsCmd.Source) serve-all"
} else {
    $argParts += "`"$python`" -m brains.control.supervisor"
    Write-Host "brains not on PATH; falling back to '$python -m brains.control.supervisor'."
    Write-Host "Consider running 'pipx install $codeDir' for a cleaner install."
}
$argument = "/c " + ($argParts -join " ")

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argument
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -Principal  $principal `
    -Description "brains supervisor: gateway + dashboard."

Write-Host "Registered scheduled task '$taskName'."
Write-Host "Manual start now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Manual stop:       Stop-ScheduledTask -TaskName $taskName"
Write-Host "Status:            Get-ScheduledTask -TaskName $taskName"
if ($env:BRAINS_STATE_DIR) {
    Write-Host "BRAINS_STATE_DIR baked into task: $env:BRAINS_STATE_DIR"
} else {
    Write-Host "No BRAINS_STATE_DIR set; supervisor will use ~/.brains/ at startup."
}
