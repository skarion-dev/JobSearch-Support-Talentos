# Nightly cycle wrapper — invoked by Task Scheduler at 00:00.
#
# $Root and $Py used to be hardcoded to one machine's paths (the dev box's
# Documents\Claude\... folder and a specific Python minor version). On the
# deployed host neither existed, so every command below failed — but with
# $ErrorActionPreference = "Continue" and no exit-code check, the script ran
# to the end anyway and PowerShell -File returned 0. Task Scheduler recorded
# a clean success every night while nothing happened: no log file, because
# even $Root\logs didn't exist to write one into. It went undetected for a
# full cycle because the previous night happened to still have a matching
# Python install; the moment that changed, it broke silently.
#
# Fixed to be self-locating (works from wherever the file physically sits)
# and to fail loudly: any error here now exits non-zero and writes to a log
# whose directory is guaranteed to exist, so a break is visible in Task
# Scheduler's last-result column instead of masquerading as success.
#
# Duplicates across runs are fine: dedupe on external_job_id/apply_url/
# source_url/company+title, and pushes are idempotent, so a repeated pull
# costs API budget but cannot create duplicate rows.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDir "nightly_$stamp.log"

try {
    $Py = (Get-Command python.exe -ErrorAction Stop).Source
    $env:PYTHONPATH = $Root
    $env:PYTHONIOENCODING = "utf-8"
    Set-Location $Root

    & $Py -m scripts.daily_cycle --keywords 500 --workers 100 *>> $LogFile
    exit $LASTEXITCODE
} catch {
    "$(Get-Date -Format o)  WRAPPER FAILED: $_" | Out-File -Append $LogFile
    exit 1
}
