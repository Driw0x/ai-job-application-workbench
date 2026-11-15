$ErrorActionPreference = "Stop"

$backendDirectory = Join-Path $PSScriptRoot "backend"
$frontendDirectory = Join-Path $PSScriptRoot "frontend"
$backendPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $backendPython)) {
    throw "Backend environment missing. Create .venv and install backend\requirements.txt."
}

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm was not found in PATH."
}

$processes = @()

try {
    $processes += Start-Process -FilePath $backendPython `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1" `
        -WorkingDirectory $backendDirectory -NoNewWindow -PassThru

    $processes += Start-Process -FilePath "npm.cmd" -ArgumentList "run", "dev" `
        -WorkingDirectory $frontendDirectory -NoNewWindow -PassThru

    Write-Host "AI Job Application Workbench started: http://127.0.0.1:5173"
    Write-Host "Press Ctrl+C to stop the backend and frontend."

    while (($processes | Where-Object { -not $_.HasExited }).Count -eq $processes.Count) {
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host "Stopping AI Job Application Workbench..."
    foreach ($process in $processes) {
        if (-not $process.HasExited) {
            taskkill.exe /PID $process.Id /T /F *> $null
        }
    }
}
