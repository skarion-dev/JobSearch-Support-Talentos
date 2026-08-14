# Streamlit app service wrapper. Binds to localhost only — Cloudflare Tunnel
# is the sole ingress, so the port is never exposed to the LAN or internet.
$Root = "C:\JobSearch-Support-Talentos"
$Py   = "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe"
$env:PYTHONPATH = $Root
$env:PYTHONIOENCODING = "utf-8"
Set-Location $Root
& $Py -m streamlit run app/main.py --server.port 3100 --server.address 127.0.0.1 --server.headless true
