# Token-based service install: the token embeds tunnel id + credentials, so the
# service does not depend on finding a config file in the right profile.
$tok = & cloudflared tunnel token jobsearch 2>$null
if (-not $tok) { "FAILED to get token"; exit 1 }

Stop-Service Cloudflared -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
& sc.exe delete Cloudflared | Out-Null
Start-Sleep -Seconds 4

& cloudflared service install $tok.Trim() 2>&1 | Select-Object -Last 2
Start-Sleep -Seconds 12

"Status   : " + (Get-Service Cloudflared -ErrorAction SilentlyContinue).Status
