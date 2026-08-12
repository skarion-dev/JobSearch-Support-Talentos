# Redeploy the gateway only: pull, install deps, restart just the gateway
# process, health-check it. Deliberately does NOT touch JobSearchApp or
# JobSearchNightly.
#
# deploy.ps1 (the app's own deploy script) stops every python.exe under the
# Python312 path to restart the app — that is a blunt match by install path,
# not by role, so running it WILL also kill this gateway process (and would
# kill a nightly cycle in flight too, though that one is at least re-run
# tomorrow). Until deploy.ps1 is made role-aware, treat any app deploy as
# also requiring `schtasks /Run /TN LLMGateway` afterward — see
# docs/GATEWAY.md.
#
# This script avoids that trap for its own restart by matching the gateway's
# process by command line (uvicorn + gateway.main:app), not just by path, so
# it never touches the app's or nightly's python.exe.
$Root = "C:\JobSearch-Support-Talentos"
$Py   = "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe"
Set-Location $Root

"== pulling =="
& git pull --ff-only
if ($LASTEXITCODE -ne 0) { "git pull failed, not restarting."; exit 1 }

"== installing any new deps =="
& $Py -m pip install -q -r requirements.txt

"== stopping existing gateway process (if any) =="
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*uvicorn*gateway.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

"== restarting gateway =="
& schtasks /Run /TN LLMGateway | Out-Null
Start-Sleep -Seconds 5
try {
    $resp = Invoke-WebRequest http://127.0.0.1:8787/healthz -UseBasicParsing -TimeoutSec 15
    "gateway responded: " + $resp.StatusCode + " " + $resp.Content
} catch {
    "GATEWAY DID NOT COME BACK, check gateway_err.log"; exit 1
}
"== deployed: " + (git log --oneline -1) + " =="
