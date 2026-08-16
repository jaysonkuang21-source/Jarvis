#Requires -Version 5.1
<#
.SYNOPSIS
  Start local Postgres+pgvector via Docker Compose and wait until healthy.

.DESCRIPTION
  Runs `docker compose up -d` from the repo root, polls until the postgres
  service is healthy, then prints the JARVIS_DATABASE_URL line to put in .env.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Get-ComposePostgresEnv {
    <#
    .SYNOPSIS
      Resolve POSTGRES_* for the URL line (env, then .env, then lab defaults).
    #>
    $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER.Trim() } else { $null }
    $pass = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD.Trim() } else { $null }
    $db = if ($env:POSTGRES_DB) { $env:POSTGRES_DB.Trim() } else { $null }

    $envFile = Join-Path $RepoRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content -Path $envFile) {
            if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
            if (-not $user -and $line -match '^\s*POSTGRES_USER\s*=\s*(.+)\s*$') {
                $user = $Matches[1].Trim().Trim('"').Trim("'")
            }
            elseif (-not $pass -and $line -match '^\s*POSTGRES_PASSWORD\s*=\s*(.+)\s*$') {
                $pass = $Matches[1].Trim().Trim('"').Trim("'")
            }
            elseif (-not $db -and $line -match '^\s*POSTGRES_DB\s*=\s*(.+)\s*$') {
                $db = $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }

    if (-not $user) { $user = "jarvis" }
    if (-not $pass) { $pass = "jarvis" }
    if (-not $db) { $db = "jarvis" }
    return @{ User = $user; Password = $pass; Db = $db }
}

function Wait-PostgresHealthy {
    <#
    .SYNOPSIS
      Poll docker compose until postgres reports healthy (or timeout).
    #>
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Creds,
        [int]$TimeoutSec = 90,
        [int]$IntervalSec = 2
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $psOut = & docker compose ps --format json 2>$null
        if ($LASTEXITCODE -eq 0 -and $psOut) {
            $healthy = $false
            foreach ($raw in @($psOut)) {
                if (-not $raw) { continue }
                try {
                    $row = $raw | ConvertFrom-Json
                } catch {
                    continue
                }
                $name = [string]$row.Service
                if (-not $name) { $name = [string]$row.Name }
                $status = [string]$row.Health
                if (-not $status) { $status = [string]$row.State }
                if ($name -match 'postgres' -and $status -match 'healthy') {
                    $healthy = $true
                    break
                }
            }
            if ($healthy) { return }
        }
        # Fallback: pg_isready inside the container.
        & docker compose exec -T postgres pg_isready -U $Creds.User -d $Creds.Db 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds $IntervalSec
    }
    throw "Postgres did not become healthy within ${TimeoutSec}s. Check: docker compose ps"
}

$creds = Get-ComposePostgresEnv

Write-Host "Starting Postgres+pgvector (docker compose up -d)..."
& docker compose up -d
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up -d failed (exit $LASTEXITCODE)."
}

Write-Host "Waiting for postgres to become healthy..."
Wait-PostgresHealthy -Creds $creds

$url = "postgresql://$($creds.User):$($creds.Password)@127.0.0.1:5432/$($creds.Db)"
Write-Host ""
Write-Host "Postgres is healthy. Set this in .env (gitignored):"
Write-Host "JARVIS_DATABASE_URL=$url"
Write-Host ""
Write-Host "Then restart the Jarvis API so it picks up the URL."
