# Launch the backend locally.
# PYTHONUTF8 avoids a Windows-console crash the first time EasyOCR downloads
# its models (it prints a block character the default cp1252 console can't
# encode).
#
# No --reload: on Windows, uvicorn's reload supervisor runs the worker on
# SelectorEventLoop instead of ProactorEventLoop, and only ProactorEventLoop
# supports subprocess creation on Windows -- which breaks our ffmpeg calls
# (NotImplementedError from create_subprocess_exec). Irrelevant on the real
# Linux deployment target; restart this script manually after code changes.
$env:PYTHONUTF8 = "1"
Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" -m uvicorn server:app --port 8000
