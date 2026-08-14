$env:PYTHONPATH = "C:\JobSearch-Support-Talentos"
Set-Location "C:\JobSearch-Support-Talentos"
Start-Process -FilePath "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe" `
    -ArgumentList @("-m","streamlit","run","app/main.py","--server.port","3100","--server.address","0.0.0.0","--server.headless","true") `
    -WorkingDirectory "C:\JobSearch-Support-Talentos" `
    -WindowStyle Hidden `
    -RedirectStandardOutput "C:\JobSearch-Support-Talentos\streamlit_out.log" `
    -RedirectStandardError "C:\JobSearch-Support-Talentos\streamlit_err.log"
Start-Sleep -Seconds 2
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, StartTime
