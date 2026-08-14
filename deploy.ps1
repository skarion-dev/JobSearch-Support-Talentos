# Deploy: pull latest code and restart the app.
# A GitHub push alone does NOT update the running system. This is what does.
$Root = "C:\JobSearch-Support-Talentos"
Set-Location $Root

"== pulling =="
& git pull --ff-only
if ($LASTEXITCODE -ne 0) { "git pull failed, not restarting."; exit 1 }

"== installing any new deps =="
& "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -q -r requirements.txt

"== restarting app =="
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*Python312*" } | Stop-Process -Force
Start-Sleep -Seconds 3
& schtasks /Run /TN JobSearchApp | Out-Null
Start-Sleep -Seconds 15

try {
    $code = (Invoke-WebRequest http://127.0.0.1:3100 -UseBasicParsing -TimeoutSec 15).StatusCode
    "app responded: $code"
} catch {
    "APP DID NOT COME BACK, check logs"; exit 1
}
"== deployed: " + (git log --oneline -1) + " =="
