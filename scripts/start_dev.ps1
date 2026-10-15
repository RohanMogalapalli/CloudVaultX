# CloudVaultX local stack launcher (Windows PowerShell 5.1+)
# Starts coordinator + 4 storage nodes + frontend dev server, detached,
# with logs written to logs\.
#
# Prereqs:
#   * PostgreSQL running with the cloudvaultx database created
#     (see scripts\setup_db.sql)
#   * .env at the repo root and a copy at backend\.env
#
# Run:  powershell -ExecutionPolicy Bypass -File scripts\start_dev.ps1
# Stop: powershell -ExecutionPolicy Bypass -File scripts\stop_dev.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
if (-not (Test-Path $py)) { throw "Python venv not found at $py" }

# --- 1. Coordinator (reads backend\.env, connects to PostgreSQL) --------
Start-Process -FilePath $py `
  -WorkingDirectory (Join-Path $root "backend") `
  -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $logs "coordinator.log") `
  -RedirectStandardError  (Join-Path $logs "coordinator.err.log")

# --- 2. Storage nodes (ports 9101-9104, each with its own data dir) -----
foreach ($i in 1..4) {
  $dataDir = Join-Path $root "node_data\n$i"
  New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

  # Start-Process children inherit these; each node captures its own copy.
  $env:NODE_NAME      = "node$i"
  $env:DATA_DIR       = $dataDir
  $env:SHARED_TOKEN   = "dev-node-token"
  $env:CAPACITY_BYTES = "1073741824"

  Start-Process -FilePath $py `
    -WorkingDirectory (Join-Path $root "storage_node") `
    -ArgumentList "-m", "uvicorn", "node_app.main:app", "--host", "127.0.0.1", "--port", "910$i" `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "node$i.log") `
    -RedirectStandardError  (Join-Path $logs "node$i.err.log")
}

# --- 3. Frontend (vite dev server; 5173, or the next free port) ---------
$frontend = Join-Path $root "frontend"
Start-Process -FilePath "cmd.exe" `
  -ArgumentList "/c", "cd /d `"$frontend`" && npm run dev > `"$logs\frontend.log`" 2>&1" `
  -WindowStyle Hidden

Write-Host ""
Write-Host "CloudVaultX stack starting:"
Write-Host "  Dashboard    : http://localhost:5173  (or 5174+ if 5173 is reserved)"
Write-Host "  API docs     : http://127.0.0.1:8000/docs"
Write-Host "  Storage nodes: 127.0.0.1:9101-9104"
Write-Host "  Logs         : logs\*.log and logs\*.err.log"
Write-Host "  Chunk map    : .venv\Scripts\python.exe scripts\show_chunks.py --email <you> --password <pw>"
