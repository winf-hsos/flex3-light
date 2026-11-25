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

With -Auto it is the scheduled task "FLEX3 Light Update" (since 2026-11-25): every quarter of an
hour, silent when there is nothing new, writing to logs\update.log only when it updates. A busy
server is not waited for long, the next run tries again. Should the new code not answer, it goes
back to the commit before by itself and notes the new one in update-failed.txt, so it is not tried
again until a newer commit arrives.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File update.ps1
powershell -ExecutionPolicy Bypass -File update.ps1 -Ref v0.2.0
powershell -ExecutionPolicy Bypass -File update.ps1 -Auto -WaitMinutes 10
#>
param(
    [string]$Root = "C:\flex3-light",
    [string]$Ref = "origin/main",
    [int]$WaitMinutes = 120,        # how long to wait for an idle server before giving up
    [switch]$Force,                 # restart even if the server stays busy or the commit is the same
    [switch]$Auto                   # run by the task: quiet, logs to logs\update.log, goes back by itself
)
$ErrorActionPreference = "Stop"
$Task = "FLEX3 Light Server"
$Code = Join-Path $Root "code"
$venvPython = Join-Path $Root "venv\Scripts\python.exe"
$Failed = Join-Path $Root "update-failed.txt"
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
if ($target -eq $current -and -not $Force) { if (-not $Auto) { Write-Host "Already at $current." }; exit 0 }
if ($Auto -and (Test-Path $Failed) -and (Get-Content $Failed -Raw).Trim() -eq $target) { exit 0 }   # did not start before
if ($Auto) {
    Start-Transcript -Path (Join-Path $Root "logs\update.log") -Append | Out-Null
    Write-Host ("{0:yyyy-MM-dd HH:mm} automatic update" -f (Get-Date))
}
$dirty = & git -C $Code status --porcelain --untracked-files=no
if ($dirty) { throw "The checkout in $Code has local changes; nothing is changed. Look at 'git -C $Code status'." }
$from = (& git -C $Code describe --tags --always HEAD).Trim()
$to = (& git -C $Code describe --tags --always $target).Trim()
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
# the task disabled first, so its five-minute trigger cannot start the old code again meanwhile;
# returns the status of the server on the commit, or $null if it does not answer
function Switch-Code([string]$commit) {
    Disable-ScheduledTask -TaskName $Task | Out-Null
    Stop-ScheduledTask -TaskName $Task
    for ($i = 0; $i -lt 30 -and (Get-ServerStatus); $i++) { Start-Sleep -Seconds 1 }     # until the port is free
    & git -C $Code checkout --detach $commit
    $checkedOut = -not $LASTEXITCODE
    if ($checkedOut) {
        & $venvPython -m pip install --quiet -r (Join-Path $Code "server\requirements.txt")
        if ($LASTEXITCODE) { Write-Host "pip install failed; starting anyway." -ForegroundColor Yellow }
    }
    Enable-ScheduledTask -TaskName $Task | Out-Null
    Start-ScheduledTask -TaskName $Task
    if (-not $checkedOut) { throw "git checkout failed; the server runs the old code again." }
    $status = $null
    for ($i = 0; $i -lt 30 -and -not $status; $i++) { Start-Sleep -Seconds 2; $status = Get-ServerStatus }
    return $status
}

$s = Switch-Code $target
if (-not $s -and $Auto) {
    Write-Host "The server does not answer on $to; back to $from."
    Set-Content -Path $Failed -Value $target -Encoding ascii
    $null = Switch-Code $current
    throw "The update to $to did not start (see $Root\logs\server.log); the server runs $from again."
}
if (-not $s) { throw "The server does not answer after the update. See $Root\logs\server.log; back with -Ref $current." }
Remove-Item $Failed -ErrorAction SilentlyContinue
Write-Host "Running $($s.version). Back to the version before: update.ps1 -Ref $current"
