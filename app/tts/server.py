"""Start / attach the local Fish Speech Docker API for Jarvis TTS."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

_MODEL = "openaudio-s1-mini"
_CONTAINER = "jarvis-fish-speech"
_IMAGE_CUDA = "jarvis-fish-speech:s1-d3df505"
_IMAGE_CPU = "jarvis-fish-speech:s1-d3df505"


def fish_data_root() -> Path:
    """Host directory mounted into the Fish Speech container."""
    path = get_settings().data_dir / "fish-speech"
    path.mkdir(parents=True, exist_ok=True)
    (path / "checkpoints").mkdir(exist_ok=True)
    (path / "references").mkdir(exist_ok=True)
    return path


def model_dir() -> Path:
    """Directory expected to hold OpenAudio S1-mini weights."""
    return fish_data_root() / "checkpoints" / _MODEL


def model_ready() -> bool:
    """True when codec weights exist on disk (minimal readiness check)."""
    return (model_dir() / "codec.pth").is_file()


def _docker_bin() -> str | None:
    """Return docker executable path, or None when Docker is unavailable."""
    return shutil.which("docker")


def _run_docker(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command and capture text output."""
    docker = _docker_bin()
    if not docker:
        raise RuntimeError("Docker not found on PATH")
    return subprocess.run(
        [docker, *args],
        check=check,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def _container_state() -> str | None:
    """Return docker state for the Fish container, or None if missing."""
    result = _run_docker(
        [
            "inspect",
            "-f",
            "{{.State.Status}}",
            _CONTAINER,
        ]
    )
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _start_existing() -> bool:
    """Start a stopped Fish container; True when start was issued successfully."""
    result = _run_docker(["start", _CONTAINER])
    if result.returncode != 0:
        logger.warning(
            "docker start %s failed: %s",
            _CONTAINER,
            (result.stderr or result.stdout or "").strip()[:300],
        )
        return False
    return True


def _create_container(*, use_cpu: bool) -> None:
    """Create and start a new Fish Speech API container with bound weights."""
    root = fish_data_root()
    checkpoints = root / "checkpoints"
    references = root / "references"
    image = _IMAGE_CPU if use_cpu else _IMAGE_CUDA
    args = [
        "run",
        "-d",
        "--name",
        _CONTAINER,
        "--restart",
        "unless-stopped",
        "-p",
        "8080:8080",
        "-v",
        f"{checkpoints}:/app/checkpoints",
        "-v",
        f"{references}:/app/references",
        "-e",
        f"LLAMA_CHECKPOINT_PATH=checkpoints/{_MODEL}",
        "-e",
        f"DECODER_CHECKPOINT_PATH=checkpoints/{_MODEL}/codec.pth",
        "-e",
        "DECODER_CONFIG_NAME=modded_dac_vq",
    ]
    if not use_cpu:
        args.extend(["--gpus", "all"])
    args.append(image)
    result = _run_docker(args)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"docker run failed: {err[:500]}")


def _wait_until_ready(timeout_seconds: float) -> bool:
    """Poll Fish Speech until reachable or timeout."""
    from app.tts import fish_reachable

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if fish_reachable():
            return True
        time.sleep(1.0)
    return fish_reachable()


def ensure_fish_speech() -> bool:
    """Ensure the Fish Speech HTTP API is up; start Docker if needed.

    Returns True when the API is reachable. Never raises for boot paths —
    callers log and continue with Web Speech fallback.
    """
    from app.tts import _base_url, fish_reachable

    settings = get_settings()
    if not settings.tts_enabled:
        return False
    if fish_reachable():
        logger.info("Fish Speech already up at %s", _base_url())
        return True
    if not settings.tts_autostart:
        logger.warning(
            "Fish Speech not reachable at %s (autostart disabled)",
            _base_url(),
        )
        return False
    if not _docker_bin():
        logger.warning(
            "Fish Speech not reachable and Docker is not on PATH; "
            "spoken replies will use Web Speech"
        )
        return False
    if not model_ready():
        logger.warning(
            "Fish Speech weights missing at %s — download OpenAudio S1-mini "
            "(see scripts/fish-speech-up.ps1) before autostart can work",
            model_dir(),
        )
        return False

    try:
        state = _container_state()
        if state == "running":
            logger.info("Fish container running but API not ready yet; waiting…")
        elif state in {"created", "exited", "paused"}:
            logger.info("Starting existing Fish Speech container (%s)…", state)
            if not _start_existing():
                return False
        else:
            use_cpu = settings.tts_fish_cpu
            logger.info(
                "Creating Fish Speech container (%s)…",
                "cpu" if use_cpu else "cuda",
            )
            _run_docker(["rm", "-f", _CONTAINER])
            try:
                _create_container(use_cpu=use_cpu)
            except RuntimeError as exc:
                if use_cpu:
                    raise
                logger.warning("%s — retrying Fish Speech on CPU image", exc)
                _run_docker(["rm", "-f", _CONTAINER])
                _create_container(use_cpu=True)

        if _wait_until_ready(settings.tts_autostart_timeout_seconds):
            logger.info("Fish Speech TTS ready at %s", _base_url())
            return True
        logger.warning(
            "Fish Speech container started but API not ready within %.0fs at %s",
            settings.tts_autostart_timeout_seconds,
            _base_url(),
        )
        return False
    except Exception:  # noqa: BLE001 — boot must not die on TTS
        logger.warning("Fish Speech autostart failed", exc_info=True)
        return False
