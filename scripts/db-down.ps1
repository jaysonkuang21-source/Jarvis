#Requires -Version 5.1
<#
.SYNOPSIS
  Stop local Postgres+pgvector started by docker compose.

.DESCRIPTION
  Runs `docker compose down` from the repo root. Named volume jarvis_pgdata
  is kept by default (data survives restarts).
#>
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

Write-Host "Stopping Postgres+pgvector (docker compose down)..."
& docker compose down
if ($LASTEXITCODE -ne 0) {
    throw "docker compose down failed (exit $LASTEXITCODE)."
}
Write-Host "Stopped. Volume jarvis_pgdata was left in place."
