<#
  Configures the "ZoteroRemarkableSync" scheduled task for an always-on watcher
  on a laptop. Run it from this folder on either computer:

      powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
          -> fixes the task's settings only. Does NOT stop the running watcher.

      powershell -ExecutionPolicy Bypass -File .\setup_task.ps1 -Restart
          -> also restarts the watcher (only needed to load new code), and stops any
             stray copies of the watcher that are running outside the scheduled task.

  Work laptop: run it from an administrator PowerShell window.

  What it sets (Windows' defaults are wrong for a watcher that should never stop):
    - runs on battery, and is NOT stopped when the laptop is unplugged
    - no 72-hour time limit (Windows otherwise kills the task after 3 days)
    - not stopped when the computer stops being idle
    - starts as soon as possible if a scheduled start was missed (e.g. asleep)
    - if the watcher ever crashes, Windows restarts it every 5 minutes
    - never runs two copies at once
#>
param([switch]$Restart)

$ErrorActionPreference = "Stop"
$TaskName = "ZoteroRemarkableSync"

function Get-WatcherProcesses {
    # Every running copy of the sync script (any folder), however it was started.
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*zotero_remarkable_sync.py*" }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' on this computer. Register it first." -ForegroundColor Red
    exit 1
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# Changing settings does not interrupt a running instance.
Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null

if ($Restart) {
    Write-Host "Restarting the watcher to load the current code..."
    Stop-ScheduledTask -TaskName $TaskName
    # Wait for the old copy to exit; starting while it is still running would be ignored.
    for ($i = 0; $i -lt 30 -and (Get-ScheduledTask -TaskName $TaskName).State -eq "Running"; $i++) {
        Start-Sleep -Seconds 1
    }
    # Stop stray copies too (e.g. one started by hand, or left over from an earlier restart).
    $strays = @(Get-WatcherProcesses)
    foreach ($p in $strays) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($strays.Count -gt 0) {
        Write-Host ("Stopped {0} stray copy/copies of the watcher." -f $strays.Count)
        Start-Sleep -Seconds 2
    }
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
}

# Report back what is actually in effect now.
$t = Get-ScheduledTask -TaskName $TaskName
$s = $t.Settings
$limit = if ($s.ExecutionTimeLimit -in @("PT0S", "", $null)) { "none" } else { $s.ExecutionTimeLimit }
$copies = @(Get-WatcherProcesses).Count
$checks = @(
    @("Exactly one watcher running ($copies)", ($copies -eq 1)),
    @("Runs on battery",            (-not $s.DisallowStartIfOnBatteries)),
    @("Keeps running when unplugged", (-not $s.StopIfGoingOnBatteries)),
    @("No time limit ($limit)",     ($limit -eq "none")),
    @("Restarts if it crashes",     ($s.RestartCount -gt 0))
)
Write-Host ""
foreach ($c in $checks) {
    $mark  = if ($c[1]) { "OK     " } else { "PROBLEM" }
    $color = if ($c[1]) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1}" -f $mark, $c[0]) -ForegroundColor $color
}
Write-Host ("  Watcher state: {0}" -f $t.State)
Write-Host ("  Runs as:       {0}" -f $t.Principal.UserId)
