# uninstall-windows.ps1 — remove the BrainsService scheduled task.
# The brains code, data dir, and logs are NOT touched.

$ErrorActionPreference = "Stop"

$taskName = "BrainsService"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'."
} else {
    Write-Host "No scheduled task '$taskName' found; nothing to remove."
}
