<#
.SYNOPSIS
Sets up the FLEX3 Light server on a Windows workstation: code, Python environment, config,
firewall rule, a scheduled task that starts the server at boot and restarts it on failure, and
one that takes every new commit from GitHub by itself once the server is idle.

.DESCRIPTION
Run once, in an elevated Windows PowerShell, as the account the server shall run under
(it runs as that account whether anyone is logged on or not, without a stored password).
Running it again is safe: it keeps the code checkout, the data and secrets.json, rewrites
config.json and replaces the firewall rule and the tasks.

Afterwards put the project's keys into secrets.json and restart the task. The guide is
docs/workstation.md.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File setup.ps1 -Url http://<workstation>:8765 -Data D:\flex3-light\data
#>
param(
    [Parameter(Mandatory = $true)][string]$Url,     # the address lecturers connect to, http(s)://host:port
    [string]$Root = "C:\flex3-light",               # code, venv, config, secrets and logs
    [string]$Data = "",                             # the server's data folder; default <Root>\data
    [int]$Port = 0,                                 # default: the port of -Url, else 8765
    [string]$RemoteAddress = "Any",                 # who may reach the port, e.g. the VPN range "10.0.0.0/8"
    [string]$Repo = "https://github.com/winf-hsos/flex3-light.git",
    [string]$Ref = "origin/main",
    [string[]]$ToolPaths = @(),                     # folders with ffmpeg.exe and typst.exe if not on PATH
    [string]$BackupTarget = ""                      # a folder on another drive for the daily backup
)
$ErrorActionPreference = "Stop"
$Task = "FLEX3 Light Server"
$BackupTask = "FLEX3 Light Backup"
$UpdateTask = "FLEX3 Light Update"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   ! $text" -ForegroundColor Yellow }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run this in an elevated PowerShell (Run as administrator)." }
if ($Url -notmatch '^https?://[^/]+') { throw "-Url must start with http:// or https://" }
$Url = $Url.TrimEnd("/")
if ($Port -eq 0) { $Port = ([Uri]$Url).Port; if ($Port -le 0) { $Port = 8765 } }
if (-not $Data) { $Data = Join-Path $Root "data" }
$Code = Join-Path $Root "code"
$Venv = Join-Path $Root "venv"
$Logs = Join-Path $Root "logs"
$Config = Join-Path $Root "config.json"
$Secrets = Join-Path $Root "secrets.json"
$User = "$env:USERDOMAIN\$env:USERNAME"

# ------------------------------------------------------------------ prerequisites
Step "Prerequisites"
$env:PATH = (($ToolPaths + @($env:PATH)) -join ";")
$missing = @()
foreach ($tool in @("git", "ffmpeg", "typst", "nvidia-smi")) {
    $found = Get-Command $tool -ErrorAction SilentlyContinue
    if ($found) { Note "$tool  $($found.Source)" } else { Warn "$tool not found"; $missing += $tool }
}
$python = $null
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    try { $python = (& py -3.14 -c "import sys; print(sys.executable)" 2>$null) } catch { $python = $null }
}
if (-not $python) {
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { $python = $p.Source }
}
if (-not $python) { throw "Python 3.14 not found. Install it from python.org (with the py launcher) and run this again." }
Note "python  $python"
if ($missing -contains "git") { throw "git is needed to fetch the code." }
if ($missing -contains "ffmpeg") { throw "ffmpeg is needed for every recording. Put its bin folder on PATH or pass -ToolPaths." }
if ($missing -contains "typst") { Warn "Without typst the summary has no PDF." }
if ($missing -contains "nvidia-smi") { Warn "No NVIDIA driver found: rendering falls back to x264 and takes a multiple of the time." }
else { & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | ForEach-Object { Note $_ } }

Step "Text recognition (Windows OCR) for German and English"
foreach ($lang in @("de-DE", "en-US")) {
    $cap = Get-WindowsCapability -Online -Name "Language.OCR~~~$lang~0.0.1.0" -ErrorAction SilentlyContinue
    if ($cap -and $cap.State -eq "Installed") { Note "$lang installed" }
    elseif ($cap) { Note "installing $lang ..."; Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    else { Warn "OCR capability for $lang not offered by this Windows" }
}

# ------------------------------------------------------------------ code and environment
Step "Folders"
foreach ($d in @($Root, $Data, $Logs)) { New-Item -ItemType Directory -Force -Path $d | Out-Null; Note $d }

Step "Code"
if (-not (Test-Path (Join-Path $Code ".git"))) { & git clone $Repo $Code; if ($LASTEXITCODE) { throw "git clone failed" } }
& git -C $Code fetch --tags origin
if ($LASTEXITCODE) { throw "git fetch failed" }
& git -C $Code checkout --detach $Ref
if ($LASTEXITCODE) { throw "git checkout $Ref failed" }
Note ("at " + (& git -C $Code describe --tags --always))

Step "Python environment"
$venvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & $python -m venv $Venv; if ($LASTEXITCODE) { throw "venv failed" } }
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $Code "server\requirements.txt")
if ($LASTEXITCODE) { throw "pip install failed" }
Note $venvPython

# ------------------------------------------------------------------ config and secrets
Step "Config"
$toolDirs = @($ToolPaths | Where-Object { $_ })
foreach ($tool in @("ffmpeg", "typst")) {
    $c = Get-Command $tool -ErrorAction SilentlyContinue
    if ($c) { $toolDirs += (Split-Path $c.Source) }
}
$cfg = [ordered]@{ data = $Data; secrets = $Secrets; host = "0.0.0.0"; port = $Port; url = $Url;
                   path = @($toolDirs | Select-Object -Unique); logs = $Logs }
[System.IO.File]::WriteAllText($Config, ($cfg | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))
Note $Config

if (-not (Test-Path $Secrets)) {
    $empty = '{"openai": "", "assemblyai": "", "elevenlabs": ""}'
    [System.IO.File]::WriteAllText($Secrets, $empty, (New-Object System.Text.UTF8Encoding $false))
    Warn "secrets.json created empty: put the project's keys in, then restart the task."
}
# only the server's account, administrators and SYSTEM may read the keys
& icacls $Secrets /inheritance:r /grant:r "${User}:(R,W)" "*S-1-5-32-544:(F)" "*S-1-5-18:(F)" | Out-Null
Note "$Secrets readable by $User and administrators only"

# ------------------------------------------------------------------ firewall
Step "Firewall"
Get-NetFirewallRule -DisplayName $Task -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $Task -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress $RemoteAddress -Action Allow -Profile Domain, Private, Public | Out-Null
Note "TCP $Port inbound from $RemoteAddress"
if ($RemoteAddress -eq "Any") { Warn "Open to every address that reaches this computer; narrow it with -RemoteAddress once the VPN range is known." }

# ------------------------------------------------------------------ scheduled tasks
Step "Scheduled task '$Task'"
$runner = Join-Path $Code "tools\workstation\run_server.py"
$action = New-ScheduledTaskAction -Execute $venvPython -Argument "`"$runner`" --config `"$Config`"" -WorkingDirectory $Code
# at startup, and every five minutes, which starts it again if it ended (a running one is left alone)
$trigger = @((New-ScheduledTaskTrigger -AtStartup),
             (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)))
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Note "runs as $User at startup, started again within five minutes if it ends"

Step "Scheduled task '$UpdateTask'"
# every quarter of an hour: a new commit of -Ref is taken once the server is idle (update.ps1 -Auto);
# conhost --headless keeps the console of PowerShell from opening on the desktop
$updater = Join-Path $Code "tools\workstation\update.ps1"
$uAction = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$updater`" -Root `"$Root`" -Ref $Ref -Auto -WaitMinutes 10"
$uTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)
$uSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $UpdateTask -Action $uAction -Trigger $uTrigger -Principal $principal `
    -Settings $uSettings -Force | Out-Null
Note "every 15 minutes: takes a new commit of $Ref once the server is idle, log in $Logs\update.log"

if ($BackupTarget) {
    Step "Scheduled task '$BackupTask'"
    $script = Join-Path $Code "tools\workstation\backup.ps1"
    $bAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$Root`" -Target `"$BackupTarget`""
    $bTrigger = New-ScheduledTaskTrigger -Daily -At "03:00"
    $bSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 6) -StartWhenAvailable
    Register-ScheduledTask -TaskName $BackupTask -Action $bAction -Trigger $bTrigger -Principal $principal `
        -Settings $bSettings -Force | Out-Null
    Note "daily at 03:00 into $BackupTarget"
}

# ------------------------------------------------------------------ start
Step "Start"
Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $Task
$status = $null
for ($i = 0; $i -lt 30 -and -not $status; $i++) {
    Start-Sleep -Seconds 2
    try { $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -TimeoutSec 5 } catch { }
}
if (-not $status) { throw "The server does not answer on port $Port. See $Logs\server.log." }
Note "running $($status.version), started $($status.started)"
Write-Host ""
Write-Host "Next: a connection code for a lecturer (an existing user id from 'users' keeps their recordings):"
Write-Host "  cd `"$Code\server`""
Write-Host "  & `"$venvPython`" -m flex3_light_server.admin --data `"$Data`" --url $Url users"
Write-Host "  & `"$venvPython`" -m flex3_light_server.admin --data `"$Data`" --url $Url invite <user id or name>"
