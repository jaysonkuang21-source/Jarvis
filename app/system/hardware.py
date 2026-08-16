"""Best-effort local hardware probe (RAM / CPU / NVIDIA GPU).

Uses stdlib only: ``ctypes`` on Windows, ``/proc`` or ``os.sysconf`` elsewhere,
and optional ``nvidia-smi`` for GPUs. Never raises to callers — failures land
in ``probe_errors``.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from typing import Any

from app.models import GpuInfo, SystemInfo

# Hardcoded argv only — never interpolate user input into nvidia-smi.
_NVIDIA_SMI_ARGS = (
    "nvidia-smi",
    "--query-gpu=name,memory.total,memory.free",
    "--format=csv,noheader,nounits",
)


def _ram_windows() -> tuple[int | None, int | None, str | None]:
    """Read total/available RAM in MiB via GlobalMemoryStatusEx."""
    class MEMORYSTATUSEX(ctypes.Structure):
        """Win32 MEMORYSTATUSEX layout for GlobalMemoryStatusEx."""

        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            return None, None, "GlobalMemoryStatusEx failed"
        total = int(stat.ullTotalPhys // (1024 * 1024))
        avail = int(stat.ullAvailPhys // (1024 * 1024))
        return total, avail, None
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return None, None, f"Windows RAM probe failed: {exc}"


def _read_text_file(path: str) -> str:
    """Read a small text file (isolated for test patching)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _ram_linux() -> tuple[int | None, int | None, str | None]:
    """Parse MemTotal / MemAvailable from /proc/meminfo."""
    try:
        text = _read_text_file("/proc/meminfo")
    except OSError as exc:
        return None, None, f"/proc/meminfo unreadable: {exc}"
    totals: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s+kB", line)
        if match:
            totals[match.group(1)] = int(match.group(2)) // 1024
    if "MemTotal" not in totals:
        return None, None, "MemTotal missing from /proc/meminfo"
    return totals.get("MemTotal"), totals.get("MemAvailable"), None


def _ram_sysconf() -> tuple[int | None, int | None, str | None]:
    """Fallback RAM estimate via os.sysconf on POSIX."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        avail_pages = os.sysconf("SC_AVPHYS_PAGES")
        total = int(pages * page_size // (1024 * 1024))
        avail = int(avail_pages * page_size // (1024 * 1024))
        return total, avail, None
    except (ValueError, OSError, AttributeError) as exc:
        return None, None, f"sysconf RAM probe failed: {exc}"


def probe_ram() -> tuple[int | None, int | None, list[str]]:
    """Return (total_mb, available_mb, errors) for the host."""
    errors: list[str] = []
    if sys.platform == "win32":
        total, avail, err = _ram_windows()
        if err:
            errors.append(err)
        return total, avail, errors
    if sys.platform.startswith("linux"):
        total, avail, err = _ram_linux()
        if err:
            errors.append(err)
            total2, avail2, err2 = _ram_sysconf()
            if err2:
                errors.append(err2)
            return total or total2, avail or avail2, errors
        return total, avail, errors
    total, avail, err = _ram_sysconf()
    if err:
        errors.append(err)
    return total, avail, errors


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    """Parse ``nvidia-smi`` csv,noheader,nounits GPU lines into GpuInfo rows."""
    gpus: list[GpuInfo] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 1:
            continue
        name = parts[0]
        total: int | None = None
        free: int | None = None
        if len(parts) >= 2:
            try:
                total = int(float(parts[1]))
            except ValueError:
                total = None
        if len(parts) >= 3:
            try:
                free = int(float(parts[2]))
            except ValueError:
                free = None
        gpus.append(GpuInfo(name=name, vram_total_mb=total, vram_free_mb=free))
    return gpus


def probe_gpus() -> tuple[list[GpuInfo], list[str]]:
    """Run nvidia-smi with a fixed argv list; return empty on miss/failure."""
    errors: list[str] = []
    if shutil.which("nvidia-smi") is None:
        return [], errors
    try:
        completed = subprocess.run(  # noqa: S603 - argv is a fixed constant tuple
            list(_NVIDIA_SMI_ARGS),
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"nvidia-smi failed: {exc}")
        return [], errors
    if completed.returncode != 0:
        errors.append(
            f"nvidia-smi exit {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:200]}"
        )
        return [], errors
    return parse_nvidia_smi_csv(completed.stdout or ""), errors


def probe_system() -> SystemInfo:
    """Collect RAM, CPU, and GPU specs without raising to the API layer."""
    probe_errors: list[str] = []
    ram_total, ram_avail, ram_errors = probe_ram()
    probe_errors.extend(ram_errors)
    gpus, gpu_errors = probe_gpus()
    probe_errors.extend(gpu_errors)
    cores: int | None
    try:
        cores = os.cpu_count()
    except Exception as exc:  # noqa: BLE001
        cores = None
        probe_errors.append(f"cpu_count failed: {exc}")
    return SystemInfo(
        ram_total_mb=ram_total,
        ram_available_mb=ram_avail,
        cpu_cores=cores,
        gpus=gpus,
        probe_errors=probe_errors,
    )


def system_info_dict() -> dict[str, Any]:
    """JSON-friendly SystemInfo for debugging."""
    return probe_system().model_dump(mode="json")
