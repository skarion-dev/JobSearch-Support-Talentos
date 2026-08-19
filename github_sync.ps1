param(
    [switch]$WhatIf
)

# Pull-only production updater for the jobs.skarion.com spare PC.
# GitHub is the source of truth; this script never pushes, resets a dirty tree,
# or accepts a divergent history. Secrets and local databases stay untracked.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir ("github_deploy_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log([string]$Message) {
    "$(Get-Date -Format o)  $Message" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

try {
    Set-Location $Root
    $env:GIT_TERMINAL_PROMPT = "0"

    # Never update code while the 7 PM matching run is executing.
    $activeAutopilot = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "scripts[\\/]autopilot_cycle\.py" }
    if ($activeAutopilot) {
        Log "SKIP: autopilot is running; deployment will retry next interval."
        exit 0
    }

    $trackedDirty = @(git status --porcelain --untracked-files=no)
    if ($trackedDirty.Count -gt 0) {
        Log "SKIP: tracked working-tree changes exist; refusing automatic deployment."
        $trackedDirty | ForEach-Object { Log "  $_" }
        exit 0
    }

    # Git writes normal fetch progress to stderr. Use cmd redirection so
    # PowerShell's Stop error mode cannot mistake that progress for failure.
    $fetchCapture = Join-Path $LogDir "github_fetch.tmp"
    $fetchCommand = 'git fetch --prune origin master > "' + $fetchCapture + '" 2>&1'
    & cmd.exe /d /c $fetchCommand
    $fetchExit = $LASTEXITCODE
    if (Test-Path $fetchCapture) {
        Get-Content $fetchCapture | ForEach-Object { Log $_ }
        Remove-Item -LiteralPath $fetchCapture -Force -ErrorAction SilentlyContinue
    }
    if ($fetchExit -ne 0) { throw "git fetch failed with exit code $fetchExit" }
    $head = (git rev-parse HEAD).Trim()
    $remote = (git rev-parse origin/master).Trim()
    if ($head -eq $remote) {
        Log "NOOP: already at $head."
        exit 0
    }

    # Refuse to discard local production commits or merge automatically.
    git merge-base --is-ancestor HEAD origin/master
    if ($LASTEXITCODE -ne 0) {
        Log "BLOCKED: local HEAD is not an ancestor of origin/master; manual reconciliation required."
        exit 2
    }

    $changedFiles = @(git diff --name-only HEAD origin/master)
    $untracked = @(git status --porcelain | Where-Object { $_ -match "^\?\? " } | ForEach-Object { $_.Substring(3) })
    foreach ($path in $changedFiles) {
        if ($untracked -contains $path) {
            Log "BLOCKED: GitHub update would collide with untracked file $path."
            exit 2
        }
    }

    if ($WhatIf) {
        Log "WHATIF: would fast-forward from $head to $remote."
        exit 0
    }

    $previous = $head
    $mergeCapture = Join-Path $LogDir "github_merge.tmp"
    $mergeCommand = 'git merge --ff-only origin/master > "' + $mergeCapture + '" 2>&1'
    & cmd.exe /d /c $mergeCommand
    $mergeExit = $LASTEXITCODE
    if (Test-Path $mergeCapture) {
        Get-Content $mergeCapture | ForEach-Object { Log $_ }
        Remove-Item -LiteralPath $mergeCapture -Force -ErrorAction SilentlyContinue
    }
    if ($mergeExit -ne 0) { throw "fast-forward failed with exit code $mergeExit" }

    # Catch syntax/import-level failures before accepting the deployment.
    python -m compileall -q app scripts
    if ($LASTEXITCODE -ne 0) { throw "Python compile check failed" }

    Start-Sleep -Seconds 3
    $health = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3100/_stcore/health -TimeoutSec 15
    if ($health.StatusCode -ne 200 -or $health.Content.Trim() -ne "ok") {
        throw "Streamlit health check failed: $($health.StatusCode) $($health.Content)"
    }
    Log "DEPLOYED: $previous -> $remote; Streamlit health=200 ok."
    exit 0
} catch {
    Log "FAILED: $_"
    try {
        if ($previous) {
            git reset --hard $previous 2>&1 | ForEach-Object { Log "ROLLBACK $_" }
            Log "ROLLED BACK to $previous."
        }
    } catch {
        Log "ROLLBACK FAILED: $_"
    }
    exit 1
}
