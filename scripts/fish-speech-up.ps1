#Requires -Version 5.1
<#
.SYNOPSIS
  Download OpenAudio S1-mini (if needed) and start the pinned Fish Speech API.

.DESCRIPTION
  Uses a locally built image jarvis-fish-speech:s1-d3df505 (Fish Speech commit
  d3df505 — last known good with OpenAudio S1-mini). Build once with
  scripts/fish-speech-build.ps1 if the image is missing.

  Jarvis autostarts this same container on boot when JARVIS_TTS_AUTOSTART=true.
#>
$ErrorActionPreference = "Continue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FishRoot = Join-Path $RepoRoot "data\fish-speech"
$Checkpoints = Join-Path $FishRoot "checkpoints"
$References = Join-Path $FishRoot "references"
$ModelDir = Join-Path $Checkpoints "openaudio-s1-mini"
$ContainerName = "jarvis-fish-speech"
$Image = "jarvis-fish-speech:s1-d3df505"
$Codec = Join-Path $ModelDir "codec.pth"

New-Item -ItemType Directory -Force -Path $Checkpoints | Out-Null
New-Item -ItemType Directory -Force -Path $References | Out-Null

if (-not (Test-Path $Codec)) {
    Write-Host "Downloading fishaudio/openaudio-s1-mini into $ModelDir ..."
    Write-Host "Requires HF access + HF_TOKEN (or huggingface-cli login)."
    Push-Location $RepoRoot
    try {
        & uv run --with "huggingface_hub" python -c @"
from huggingface_hub import snapshot_download
from pathlib import Path
import os
dest = Path(r'$ModelDir')
dest.mkdir(parents=True, exist_ok=True)
token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGING_FACE_HUB_TOKEN') or os.environ.get('JARVIS_HF_TOKEN')
snapshot_download(repo_id='fishaudio/openaudio-s1-mini', local_dir=str(dest), token=token)
print('download ok', dest)
"@
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path $Codec)) {
    Write-Host "codec.pth still missing: $Codec"
    exit 1
}

$imgId = docker images -q $Image 2>$null
if (-not $imgId) {
    Write-Host "Image $Image missing - building (first time, 10-20 min)..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "fish-speech-build.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$inspectOut = & docker inspect -f "{{.State.Status}}" $ContainerName 2>$null
$inspectCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap

$state = $null
if ($inspectCode -eq 0 -and $inspectOut) {
    $state = ("$inspectOut").Trim()
}

if ($state -eq "running") {
    Write-Host "Fish Speech already running ($ContainerName)."
    Write-Host "API: http://127.0.0.1:8080/docs"
    exit 0
}
if ($state) {
    Write-Host "Starting existing container $ContainerName (was $state)..."
    docker start $ContainerName | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "API: http://127.0.0.1:8080/docs"
        exit 0
    }
}

docker rm -f $ContainerName 2>$null | Out-Null

Write-Host "Starting $Image as $ContainerName ..."
docker run -d `
    --name $ContainerName `
    --restart unless-stopped `
    --gpus all `
    -p 8080:8080 `
    -v "${Checkpoints}:/app/checkpoints" `
    -v "${References}:/app/references" `
    -e "LLAMA_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini" `
    -e "DECODER_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini/codec.pth" `
    -e "DECODER_CONFIG_NAME=modded_dac_vq" `
    $Image

if ($LASTEXITCODE -ne 0) {
    Write-Host "GPU run failed; retrying without --gpus ..."
    docker rm -f $ContainerName 2>$null | Out-Null
    docker run -d `
        --name $ContainerName `
        --restart unless-stopped `
        -p 8080:8080 `
        -v "${Checkpoints}:/app/checkpoints" `
        -v "${References}:/app/references" `
        -e "LLAMA_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini" `
        -e "DECODER_CHECKPOINT_PATH=checkpoints/openaudio-s1-mini/codec.pth" `
        -e "DECODER_CONFIG_NAME=modded_dac_vq" `
        -e "BACKEND=cpu" `
        $Image
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host @"
Fish Speech API: http://127.0.0.1:8080 (docs: /docs)
Jarvis autostarts this container on boot when it is stopped.
Stop: docker stop $ContainerName
"@
