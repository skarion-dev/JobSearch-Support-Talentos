param(
    [switch]$DryRun,
    [switch]$SkipIngest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDir "autopilot_$stamp.log"

try {
    $Py = (Get-Command python.exe -ErrorAction Stop).Source
    $env:PYTHONPATH = $Root
    $env:PYTHONIOENCODING = "utf-8"
    Set-Location $Root
    $ErrorActionPreference = "Continue"
    $Args = @()
    if ($DryRun) { $Args += "--dry-run" }
    if ($SkipIngest) { $Args += "--skip-ingest" }
    & $Py -u -m scripts.autopilot_cycle @Args *>> $LogFile
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "autopilot_cycle.py exited with code $code" }
} catch {
    "$(Get-Date -Format o)  AUTOPILOT FAILED: $_" | Out-File -Append $LogFile
    exit 1
}
exit 0
