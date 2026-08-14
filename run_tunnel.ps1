$env:TUNNEL_ORIGIN_CERT = "C:\Users\saki-\.cloudflared\cert.pem"
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" `
    --config "C:\Users\saki-\.cloudflared\config.yml" `
    --no-autoupdate tunnel run jobsearch
