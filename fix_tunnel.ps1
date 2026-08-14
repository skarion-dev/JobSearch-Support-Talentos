$exe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$cfg = "C:\Windows\System32\config\systemprofile\.cloudflared\config.yml"
$bin = '"{0}" --config "{1}" --no-autoupdate tunnel run jobsearch' -f $exe, $cfg

Stop-Service Cloudflared -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
# sc.exe requires a space after binPath=
& sc.exe config Cloudflared binPath= "$bin" | Out-Null
& sc.exe failure Cloudflared reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
Start-Sleep -Seconds 2
Start-Service Cloudflared
Start-Sleep -Seconds 15

"PathName : " + (Get-CimInstance Win32_Service -Filter "Name='Cloudflared'").PathName
"Status   : " + (Get-Service Cloudflared).Status
