$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Chưa có .venv. Xem README.md để cài môi trường Python.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
    throw 'Chưa build giao diện. Chạy pnpm build trong frontend trước.'
}
& $pythonExecutable -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
