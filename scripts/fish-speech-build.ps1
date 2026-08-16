#Requires -Version 5.1
<#
.SYNOPSIS
  Build the pinned Fish Speech server image for OpenAudio S1-mini.

.DESCRIPTION
  Clones/checks out fishaudio/fish-speech @ d3df505 (if needed) and builds
  jarvis-fish-speech:s1-d3df505. First build can take 10–20+ minutes.
#>
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Src = Join-Path $RepoRoot "third_party\fish-speech"
$Commit = "d3df50503b36314a964f66cac1af1e19e95bcfa3"
$Image = "jarvis-fish-speech:s1-d3df505"

if (-not (Test-Path (Join-Path $Src "docker\Dockerfile"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Src) | Out-Null
    git clone --depth 1 https://github.com/fishaudio/fish-speech.git $Src
}

Push-Location $Src
try {
    git fetch --depth 1 origin $Commit
    git checkout $Commit
    Write-Host "Building $Image from $Commit (this takes a while)..."
    docker build `
        --platform linux/amd64 `
        -f docker/Dockerfile `
        --build-arg BACKEND=cuda `
        --build-arg CUDA_VER=12.6.0 `
        --build-arg UV_EXTRA=cu126 `
        --target server `
        -t $Image `
        .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Built $Image"
} finally {
    Pop-Location
}
