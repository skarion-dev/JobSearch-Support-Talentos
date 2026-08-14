# Clean up any half-created service, then run the tunnel as a scheduled task.
# Scheduled tasks are already proven on this host and avoid the service
# registry/config-discovery problems entirely.
Stop-Service Cloudflared -Force -ErrorAction SilentlyContinue
& sc.exe delete Cloudflared 2>&1 | Out-Null
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

$run = "C:\JobSearch-Support-Talentos\run_tunnel.ps1"
@'
$env:TUNNEL_ORIGIN_CERT = "C:\Users\saki-\.cloudflared\cert.pem"
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" `
    --config "C:\Users\saki-\.cloudflared\config.yml" `
    --no-autoupdate tunnel run jobsearch
'@ | Set-Content -Path $run -Encoding UTF8

schtasks /Delete /TN CloudflareTunnel /F 2>&1 | Out-Null
schtasks /Create /TN CloudflareTunnel /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File $run" /SC ONSTART /RU "saki-" /RL HIGHEST /F | Out-Null
schtasks /Run /TN CloudflareTunnel | Out-Null
Start-Sleep -Seconds 20

"cloudflared processes: " + (Get-Process cloudflared -ErrorAction SilentlyContinue).Count
