# Starts the LLM gateway, detached, matching launch_app.ps1's pattern.
# Registered as scheduled task LLMGateway (ONSTART) on the server — see
# docs/GATEWAY.md for the schtasks command.
#
# --workers 1 is not a tuning knob: gateway/keys.py's rotation/cooldown state
# lives in process memory, so a second worker would have its own view of
# which upstream keys are healthy. Do not raise this without moving that
# state into gateway/gateway.db first.
$Root = "C:\JobSearch-Support-Talentos"
$Py   = "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe"

$env:PYTHONPATH = $Root
Set-Location $Root

Start-Process -FilePath $Py `
    -ArgumentList @("-m","uvicorn","gateway.main:app","--host","127.0.0.1","--port","8787","--workers","1") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$Root\gateway_out.log" `
    -RedirectStandardError "$Root\gateway_err.log"

Start-Sleep -Seconds 3
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, StartTime
