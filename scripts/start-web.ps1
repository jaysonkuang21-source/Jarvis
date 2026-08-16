#Requires -Version 5.1
<#
.SYNOPSIS
  Start Jarvis FastAPI + Vite and open the web UI.

.DESCRIPTION
  Resolves the repo root from this script's location, starts the backend and
  frontend as child processes, waits for Vite and /api/health, opens the
  default browser, then waits until Ctrl+C (or window close) and kills children.
  Reuses healthy services already on 5173/8756; reclaiming only stale listeners.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$FrontendDir = Join-Path $RepoRoot "frontend"
$ViteUrl = "http://127.0.0.1:5173"
$HealthUrl = "http://127.0.0.1:8756/api/health"
$PollTimeoutSec = 60

$script:BackendProc = $null
$script:FrontendProc = $null
$script:OwnChildren = $false

function Get-PortListeners {
    <#
    .SYNOPSIS
      Return listening PIDs and process names for a local TCP port.
    #>
    param([Parameter(Mandatory = $true)][int]$Port)
    $rows = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in @($conns)) {
            $procName = $null
            try {
                $procName = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            } catch { }
            $rows += @{
                localAddress = "$($c.LocalAddress)"
                pid          = $c.OwningProcess
                processName  = $procName
            }
        }
    } catch { }
    return $rows
}

function Stop-PortListeners {
    <#
    .SYNOPSIS
      Force-stop processes listening on the given local TCP ports (timed; no hang).
    #>
    param([Parameter(Mandatory = $true)][int[]]$Ports)
    $pids = @{}
    foreach ($port in $Ports) {
        foreach ($row in @(Get-PortListeners -Port $port)) {
            if ($row.pid -and $row.pid -gt 0) {
                $pids[[int]$row.pid] = "$($row.processName)"
            }
        }
    }
    # Never use $pid — it is PowerShell's automatic read-only process id.
    foreach ($holderPid in @($pids.Keys)) {
        $name = $pids[$holderPid]
        Write-Host "Releasing port holder PID $holderPid ($name)..."
        # Avoid taskkill /T — tree kills can hang on uv/npm process trees.
        try {
            $tk = Start-Process -FilePath "taskkill.exe" `
                -ArgumentList @("/PID", "$holderPid", "/F") `
                -WindowStyle Hidden -PassThru
            if (-not $tk.WaitForExit(5000)) {
                try { $tk.Kill() } catch { }
            }
        } catch { }
        try {
            Stop-Process -Id $holderPid -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    return @($pids.Keys)
}

function Wait-PortsFree {
    <#
    .SYNOPSIS
      Wait until none of the ports have a Listen socket.
    #>
    param(
        [Parameter(Mandatory = $true)][int[]]$Ports,
        [int]$TimeoutSec = 20
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $busy = @()
        foreach ($port in $Ports) {
            if (@(Get-PortListeners -Port $port).Count -gt 0) {
                $busy += $port
            }
        }
        if ($busy.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Get-ExistingApiToken {
    <#
    .SYNOPSIS
      Return JARVIS_API_TOKEN from process env or repo .env, else $null.
      Never invents a token — reuse paths must align Vite to the live API secret.
    #>
    if ($env:JARVIS_API_TOKEN -and $env:JARVIS_API_TOKEN.Trim().Length -gt 0) {
        return $env:JARVIS_API_TOKEN.Trim()
    }
    $envFile = Join-Path $RepoRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content -Path $envFile) {
            if ($line -match '^\s*JARVIS_API_TOKEN\s*=\s*(.+)\s*$') {
                $val = $Matches[1].Trim().Trim('"').Trim("'")
                if ($val.Length -gt 0) { return $val }
            }
        }
    }
    return $null
}

function New-ApiToken {
    <#
    .SYNOPSIS
      Mint a fresh random API token (hex).
    #>
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

function Get-OrMintApiToken {
    <#
    .SYNOPSIS
      Prefer an existing JARVIS_API_TOKEN (env or .env); otherwise mint one.
    #>
    $existing = Get-ExistingApiToken
    if ($null -ne $existing) { return $existing }
    return New-ApiToken
}

function Stop-ChildTree {
    param([System.Diagnostics.Process]$Proc)
    if ($null -eq $Proc) { return }
    try {
        if (-not $Proc.HasExited) {
            # /T kills the whole tree (uv/npm spawn grandchildren).
            & taskkill.exe /PID $Proc.Id /T /F 2>$null | Out-Null
        }
    } catch {
        # Best-effort cleanup on exit.
    }
}

function Stop-AllChildren {
    Stop-ChildTree -Proc $script:FrontendProc
    Stop-ChildTree -Proc $script:BackendProc
}

function Test-HttpReady {
    <#
    .SYNOPSIS
      True when URL returns an HTTP 2xx–4xx within a short timeout.
    #>
    param([string]$Url)
    # Prefer curl.exe: Invoke-WebRequest under ErrorAction Stop can false-negative
    # on localhost (proxy/IE settings).
    try {
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($null -ne $curl) {
            $code = & curl.exe -s -o NUL -w "%{http_code}" --connect-timeout 2 --max-time 3 $Url 2>$null
            if ($LASTEXITCODE -eq 0 -and $code -match '^\d+$') {
                $n = [int]$code
                return $n -ge 200 -and $n -lt 500
            }
            return $false
        }
    } catch {
        # fall through
    }
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Test-ViteReady {
    <#
    .SYNOPSIS
      True when Vite answers on 127.0.0.1, localhost, or ::1.
    #>
    $candidates = @(
        "http://127.0.0.1:5173/",
        "http://localhost:5173/",
        "http://[::1]:5173/"
    )
    foreach ($u in $candidates) {
        if (Test-HttpReady -Url $u) { return $true }
    }
    return $false
}

function Test-OwnedChildFailed {
    <#
    .SYNOPSIS
      True when a process we spawned has already exited.
    #>
    if ($null -ne $script:BackendProc -and $script:BackendProc.HasExited) { return $true }
    if ($null -ne $script:FrontendProc -and $script:FrontendProc.HasExited) { return $true }
    return $false
}

function Start-CmdChild {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    # cmd.exe so .cmd shims (npm) resolve on PATH the same way a terminal does.
    return Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/c", $CommandLine) `
        -WorkingDirectory $WorkingDirectory `
        -PassThru `
        -NoNewWindow
}

try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is not on PATH. Install uv, then run: uv sync"
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm is not on PATH. Install Node.js, then retry."
    }

    $nodeModules = Join-Path $FrontendDir "node_modules"
    if (-not (Test-Path $nodeModules)) {
        Write-Host "frontend/node_modules missing - running npm install..."
        Push-Location $FrontendDir
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) {
                throw "npm install failed with exit code $LASTEXITCODE"
            }
        } finally {
            Pop-Location
        }
    }

    $viteAlready = Test-ViteReady
    $apiAlready = Test-HttpReady -Url $HealthUrl
    $listeners5173 = @(Get-PortListeners -Port 5173)
    $listeners8756 = @(Get-PortListeners -Port 8756)

    # Shared secret for both children. Never mint a Vite-only token while an
    # already-listening API keeps a different secret — restart the API instead.
    $existingToken = Get-ExistingApiToken
    $needBackend = -not $apiAlready
    $needFrontend = -not $viteAlready

    if ($apiAlready -and ($null -eq $existingToken)) {
        Write-Host "Healthy API on $HealthUrl has no JARVIS_API_TOKEN in env/.env — restarting API with a minted shared token."
        [void](Stop-PortListeners -Ports @(8756))
        if (-not (Wait-PortsFree -Ports @(8756) -TimeoutSec 20)) {
            throw "Could not free port 8756 to restart API with aligned token."
        }
        $apiAlready = $false
        $needBackend = $true
        $apiToken = New-ApiToken
        # Vite bakes VITE_* at process start — force restart with the shared mint.
        if ($viteAlready) {
            Write-Host "Restarting Vite so it receives the same minted API token."
            [void](Stop-PortListeners -Ports @(5173))
            if (-not (Wait-PortsFree -Ports @(5173) -TimeoutSec 20)) {
                throw "Could not free port 5173 to restart Vite with aligned token."
            }
            $viteAlready = $false
            $needFrontend = $true
        }
    } elseif ($apiAlready) {
        $apiToken = $existingToken
    } else {
        $apiToken = Get-OrMintApiToken
    }

    if ($viteAlready -and $apiAlready -and (-not $needBackend) -and (-not $needFrontend)) {
        Write-Host "Jarvis already running on $ViteUrl - opening browser (Ctrl+C closes this window only)."
        Start-Process $ViteUrl
        Write-Host ""
        Write-Host "Jarvis web UI (reuse) - Ctrl+C to exit this launcher"
        Write-Host ""
        while ($true) {
            if (-not (Test-ViteReady) -or -not (Test-HttpReady -Url $HealthUrl)) {
                Write-Host "Existing servers stopped responding; exiting launcher."
                break
            }
            Start-Sleep -Seconds 2
        }
        return
    }

    # Only reclaim listeners that are NOT serving HTTP — never kill a healthy API/Vite.
    $portsToFree = @()
    if (($listeners5173.Count -gt 0) -and (-not $viteAlready)) { $portsToFree += 5173 }
    if (($listeners8756.Count -gt 0) -and (-not $apiAlready) -and $needBackend) {
        # Already reclaimed above when restarting for token alignment.
        if (@(Get-PortListeners -Port 8756).Count -gt 0) { $portsToFree += 8756 }
    }
    if ($portsToFree.Count -gt 0) {
        $portsToFree = @($portsToFree | Select-Object -Unique)
        Write-Host "Stale listeners without HTTP - reclaiming $($portsToFree -join ', ')..."
        [void](Stop-PortListeners -Ports $portsToFree)
        if (-not (Wait-PortsFree -Ports $portsToFree -TimeoutSec 20)) {
            throw "Could not free ports $($portsToFree -join ', ') after stopping holders. Close tauri/dev terminals and retry."
        }
    }

    $env:JARVIS_API_TOKEN = $apiToken
    $env:VITE_JARVIS_API_TOKEN = $apiToken
    $env:JARVIS_ALLOW_UNAUTHENTICATED_API = "false"

    if ($needBackend) {
        Write-Host "Starting FastAPI (uv run python -m app.main)..."
        $script:BackendProc = Start-CmdChild -CommandLine "uv run python -m app.main" -WorkingDirectory $RepoRoot
    } else {
        Write-Host "Reusing healthy API on $HealthUrl (token aligned from env/.env)"
    }

    if ($needFrontend) {
        Write-Host "Starting Vite (npm run dev -- --host 127.0.0.1)..."
        $script:FrontendProc = Start-CmdChild -CommandLine "npm run dev -- --host 127.0.0.1" -WorkingDirectory $FrontendDir
    } else {
        Write-Host "Reusing healthy Vite on $ViteUrl"
    }
    $script:OwnChildren = ($needBackend -or $needFrontend)

    if ($needFrontend) {
        Write-Host "Waiting for $ViteUrl (up to ${PollTimeoutSec}s)..."
        $deadline = (Get-Date).AddSeconds($PollTimeoutSec)
        $viteReady = $false
        while ((Get-Date) -lt $deadline) {
            if ($null -ne $script:BackendProc -and $script:BackendProc.HasExited) {
                throw "Backend exited early (code $($script:BackendProc.ExitCode)). Check API logs / .env."
            }
            if ($null -ne $script:FrontendProc -and $script:FrontendProc.HasExited) {
                throw "Frontend exited early (code $($script:FrontendProc.ExitCode)). Check Vite output."
            }
            if (Test-ViteReady) {
                $viteReady = $true
                break
            }
            Start-Sleep -Milliseconds 500
        }

        if (-not $viteReady) {
            throw "Timed out waiting for $ViteUrl after ${PollTimeoutSec}s."
        }
    }

    Write-Host "Waiting for $HealthUrl (up to ${PollTimeoutSec}s)..."
    $deadline = (Get-Date).AddSeconds($PollTimeoutSec)
    $healthReady = $false
    while ((Get-Date) -lt $deadline) {
        if ($null -ne $script:BackendProc -and $script:BackendProc.HasExited) {
            throw "Backend exited early (code $($script:BackendProc.ExitCode)). Check API logs / .env."
        }
        if ($null -ne $script:FrontendProc -and $script:FrontendProc.HasExited) {
            throw "Frontend exited early (code $($script:FrontendProc.ExitCode)). Check Vite output."
        }
        # HTTP status only — do not require body ok/healthy (degraded vault/Obsidian is fine).
        if (Test-HttpReady -Url $HealthUrl) {
            $healthReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $healthReady) {
        throw "Timed out waiting for $HealthUrl after ${PollTimeoutSec}s."
    }

    Write-Host "Opening $ViteUrl"
    Start-Process $ViteUrl

    Write-Host ""
    Write-Host "Jarvis web UI - Ctrl+C to stop"
    Write-Host ""

    while ($true) {
        if (Test-OwnedChildFailed) {
            Write-Host "A child process exited; shutting down."
            break
        }
        if (-not $script:OwnChildren) {
            if (-not (Test-ViteReady) -or -not (Test-HttpReady -Url $HealthUrl)) {
                Write-Host "Servers stopped responding; exiting launcher."
                break
            }
        }
        Start-Sleep -Seconds 1
    }
} finally {
    if ($script:OwnChildren) {
        Stop-AllChildren
    }
}
