<#
.SYNOPSIS
Brings the FLEX3 Light server on the workstation to another commit or tag, without cutting
off a lecture that is being joined or a pipeline step that is running.

.DESCRIPTION
Fetches the code, waits until the server reports twice in a row that it is idle (GET /status:
nothing assembling, nothing running, nothing queued), stops the scheduled task, checks out the
new commit, installs the requirements and starts the task again. An upload in progress is no
reason to wait: the app sends the missing parts again once the server answers.

Run it as the account that ran setup.ps1. To go back, run it with -Ref and the commit it
names as the one before.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File update.ps1
powershell -ExecutionPolicy Bypass -File update.ps1 -Ref v0.2.0
#>
param(
    [string]$Root = "C:\flex3-light",
    [string]$Ref = "origin/main",
    [int]$WaitMinutes = 120,        # how long to wait for an idle server before giving up
    [switch]$Force                  # restart even if the server stays busy or the commit is the same
)
$ErrorActionPreference = "Stop"
$Task = "FLEX3 Light Server"
$Code = Join-Path $Root "code"
$venvPython = Join-Path $Root "venv\Scripts\python.exe"
$cfg = Get-Content (Join-Path $Root "config.json") -Raw | ConvertFrom-Json
$StatusUrl = "http://127.0.0.1:$($cfg.port)/status"

function Get-ServerStatus {
    try { return Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 10 } catch { return $null }
}

& git -C $Code fetch --tags origin
if ($LASTEXITCODE) { throw "git fetch failed" }
$target = (& git -C $Code rev-parse "$Ref^{commit}").Trim()
if ($LASTEXITCODE) { throw "unknown ref $Ref" }
$current = (& git -C $Code rev-parse HEAD).Trim()
$dirty = & git -C $Code status --porcelain --untracked-files=no
if ($dirty) { throw "The checkout in $Code has local changes; nothing is changed. Look at 'git -C $Code status'." }
$from = (& git -C $Code describe --tags --always HEAD).Trim()
$to = (& git -C $Code describe --tags --always $target).Trim()
if ($target -eq $current -and -not $Force) { Write-Host "Already at $from."; exit 0 }
Write-Host "Update $from -> $to"
& git -C $Code log --oneline "$current..$target" | Select-Object -Last 20 | ForEach-Object { Write-Host "  $_" }

# ------------------------------------------------------------------ wait until idle
$deadline = (Get-Date).AddMinutes($WaitMinutes)
$idleOnce = $false
while ($true) {
    $s = Get-ServerStatus
    if (-not $s) { Write-Host "The server does not answer; going on."; break }
    if (-not $s.busy) {
        if ($idleOnce) { break }
        $idleOnce = $true
        Start-Sleep -Seconds 15
        continue
    }
    $idleOnce = $false
    if ((Get-Date) -gt $deadline) {
        if ($Force) { Write-Host "Still busy after $WaitMinutes minutes; restarting anyway (-Force)."; break }
        throw "Still busy after $WaitMinutes minutes (running: $($s.active), queued: $($s.queued), assembling: $($s.assembling)). Nothing changed."
    }
    Write-Host ("{0:HH:mm} busy: running {1}, queued {2}, assembling {3}; waiting" -f (Get-Date), $s.active, $s.queued, $s.assembling)
    Start-Sleep -Seconds 30
}

# ------------------------------------------------------------------ swap the code
# disabled first, so its five-minute trigger cannot start the old code again meanwhile
Disable-ScheduledTask -TaskName $Task | Out-Null
Stop-ScheduledTask -TaskName $Task
for ($i = 0; $i -lt 30 -and (Get-ServerStatus); $i++) { Start-Sleep -Seconds 1 }     # until the port is free
& git -C $Code checkout --detach $target
if ($LASTEXITCODE) {
    Enable-ScheduledTask -TaskName $Task | Out-Null; Start-ScheduledTask -TaskName $Task
    throw "git checkout failed; the server runs the old code again."
}
& $venvPython -m pip install --quiet -r (Join-Path $Code "server\requirements.txt")
if ($LASTEXITCODE) { Write-Host "pip install failed; starting anyway." -ForegroundColor Yellow }
Enable-ScheduledTask -TaskName $Task | Out-Null
Start-ScheduledTask -TaskName $Task

$s = $null
for ($i = 0; $i -lt 30 -and -not $s; $i++) { Start-Sleep -Seconds 2; $s = Get-ServerStatus }
if (-not $s) { throw "The server does not answer after the update. See $Root\logs\server.log; back with -Ref $current." }
Write-Host "Running $($s.version). Back to the version before: update.ps1 -Ref $current"
