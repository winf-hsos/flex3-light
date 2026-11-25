<#
.SYNOPSIS
Copies the FLEX3 Light server's data, config and keys to a folder on another drive.

.DESCRIPTION
Run daily by the task setup.ps1 registers with -BackupTarget. The copy only adds and
updates, it never deletes: a recording deleted on the server stays in the backup until
someone removes it there. accounts.sqlite is copied through SQLite's own backup, so a
write during the copy cannot tear it. The log is backup.log in the logs folder.
#>
param(
    [string]$Root = "C:\flex3-light",
    [Parameter(Mandatory = $true)][string]$Target
)
$ErrorActionPreference = "Stop"
$cfg = Get-Content (Join-Path $Root "config.json") -Raw | ConvertFrom-Json
$log = Join-Path $cfg.logs "backup.log"
New-Item -ItemType Directory -Force -Path (Join-Path $Target "data"), (Join-Path $Target "config") | Out-Null

& robocopy $cfg.data (Join-Path $Target "data") /E /XD logs /XF "accounts.sqlite*" /R:2 /W:5 /NP /NFL /NDL "/LOG+:$log" | Out-Null
$copied = $LASTEXITCODE
& robocopy $Root (Join-Path $Target "config") config.json secrets.json /R:2 /W:5 /NP "/LOG+:$log" | Out-Null

$venvPython = Join-Path $Root "venv\Scripts\python.exe"
& $venvPython -c "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" `
    (Join-Path $cfg.data "accounts.sqlite") (Join-Path $Target "data\accounts.sqlite")
$accounts = $LASTEXITCODE

Add-Content -Path $log -Value ("{0:yyyy-MM-dd HH:mm} robocopy {1}, accounts {2}" -f (Get-Date), $copied, $accounts)
if ($copied -ge 8 -or $accounts -ne 0) { exit 1 }     # robocopy: 0 to 7 is success
exit 0
