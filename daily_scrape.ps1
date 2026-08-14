$env:PYTHONPATH = "C:\JobSearch-Support-Talentos"
Set-Location "C:\JobSearch-Support-Talentos"
& "C:\Users\saki-\AppData\Local\Programs\Python\Python312\python.exe" -m scripts.daily_scrape --pages 40 *>> "C:\JobSearch-Support-Talentos\daily_scrape.log"
