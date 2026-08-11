# Nightly cycle wrapper — invoked by Task Scheduler at 00:00.
# Duplicates across runs are fine: dedupe on external_job_id/apply_url/
# source_url/company+title, and pushes are idempotent, so a repeated pull
# costs API budget but cannot create duplicate rows.

$ErrorActionPreference = "Continue"
$Root = "C:\Users\sakis\Documents\Claude\JobSearch-Support-Talentos"
$Py   = "C:\Users\sakis\AppData\Local\Programs\Python\Python314\python.exe"

$env:PYTHONPATH = $Root
$env:PYTHONIOENCODING = "utf-8"
Set-Location $Root

$stamp = Get-Date -Format "yyyy-MM-dd"
& $Py -m scripts.daily_cycle --keywords 500 --workers 100 *>> "$Root\logs\nightly_$stamp.log"
