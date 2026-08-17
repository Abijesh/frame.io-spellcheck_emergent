# Launch the backend locally.
# PYTHONUTF8 avoids a Windows-console crash the first time EasyOCR downloads
# its models (it prints a block character the default cp1252 console can't
# encode).
$env:PYTHONUTF8 = "1"
Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" -m uvicorn server:app --reload --port 8000
