# CloudVaultX: stop the local dev stack started by start_dev.ps1.
# Stops the coordinator, the 4 storage nodes and the vite dev server by
# command-line match (ports can drift, e.g. vite moving to 5174).

$ErrorActionPreference = "SilentlyContinue"

# Coordinator + storage nodes (uvicorn processes of this project).
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match "app\.main:app|node_app\.main:app" } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Host "Stopped python PID $($_.ProcessId)"
  }

# Frontend dev server.
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match "vite" } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Host "Stopped vite PID $($_.ProcessId)"
  }

Write-Host "CloudVaultX dev stack stopped."
