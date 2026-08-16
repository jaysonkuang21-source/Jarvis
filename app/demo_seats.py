"""In-memory demo seat leases: cap concurrent anonymous users per client IP."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass

_SEAT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class SeatClaimResult:
    """Outcome of claiming or refreshing a demo seat."""

    ok: bool
    seat_id: str | None = None
    detail: str | None = None
    seats_used: int = 0
    seats_max: int = 0


class DemoSeatRegistry:
    """Track active demo seats keyed by client IP (sliding TTL)."""

    def __init__(self, *, max_per_ip: int = 4, ttl_seconds: float = 1800.0) -> None:
        """Create an empty registry with the given capacity and idle TTL."""
        self._max_per_ip = max(1, max_per_ip)
        self._ttl = max(60.0, ttl_seconds)
        self._lock = asyncio.Lock()
        # ip -> {seat_id: last_seen_monotonic}
        self._by_ip: dict[str, dict[str, float]] = {}

    @property
    def max_per_ip(self) -> int:
        """Configured seat capacity per IP."""
        return self._max_per_ip

    def is_valid_seat_id(self, seat_id: str) -> bool:
        """True when ``seat_id`` looks like a UUID string."""
        return bool(_SEAT_ID_RE.match(seat_id.strip()))

    def _prune(self, seats: dict[str, float], now: float) -> None:
        """Drop expired seats from one IP bucket in place."""
        cutoff = now - self._ttl
        expired = [sid for sid, seen in seats.items() if seen < cutoff]
        for sid in expired:
            del seats[sid]

    async def claim(self, ip: str, seat_id: str | None) -> SeatClaimResult:
        """Refresh or allocate a seat for ``ip``.

        Existing valid seat ids are always refreshed. A new seat is allocated
        when under capacity; otherwise the claim fails.
        """
        host = (ip or "unknown").strip() or "unknown"
        requested = (seat_id or "").strip()
        if requested and not self.is_valid_seat_id(requested):
            return SeatClaimResult(
                ok=False,
                detail="Invalid demo seat id",
                seats_max=self._max_per_ip,
            )

        now = time.monotonic()
        async with self._lock:
            seats = self._by_ip.setdefault(host, {})
            self._prune(seats, now)
            if not seats and host in self._by_ip and not self._by_ip[host]:
                # Keep empty dict; cleaned below if still empty after claim miss.
                pass

            if requested and requested in seats:
                seats[requested] = now
                return SeatClaimResult(
                    ok=True,
                    seat_id=requested,
                    seats_used=len(seats),
                    seats_max=self._max_per_ip,
                )

            if requested and requested not in seats:
                # Stale client id after expiry: treat as a new claim with that id
                # when capacity remains so refresh after TTL is smooth.
                if len(seats) >= self._max_per_ip:
                    return SeatClaimResult(
                        ok=False,
                        detail=(
                            f"Demo seat limit reached for this network "
                            f"({self._max_per_ip} concurrent users)."
                        ),
                        seats_used=len(seats),
                        seats_max=self._max_per_ip,
                    )
                seats[requested] = now
                return SeatClaimResult(
                    ok=True,
                    seat_id=requested,
                    seats_used=len(seats),
                    seats_max=self._max_per_ip,
                )

            if len(seats) >= self._max_per_ip:
                return SeatClaimResult(
                    ok=False,
                    detail=(
                        f"Demo seat limit reached for this network "
                        f"({self._max_per_ip} concurrent users)."
                    ),
                    seats_used=len(seats),
                    seats_max=self._max_per_ip,
                )

            new_id = str(uuid.uuid4())
            seats[new_id] = now
            return SeatClaimResult(
                ok=True,
                seat_id=new_id,
                seats_used=len(seats),
                seats_max=self._max_per_ip,
            )


_registry: DemoSeatRegistry | None = None


def get_demo_seat_registry(
    *, max_per_ip: int | None = None, ttl_seconds: float | None = None
) -> DemoSeatRegistry:
    """Return the process-wide demo seat registry, creating it on first use."""
    global _registry
    if _registry is None:
        from app.config import get_settings

        settings = get_settings()
        _registry = DemoSeatRegistry(
            max_per_ip=max_per_ip
            if max_per_ip is not None
            else settings.demo_max_seats_per_ip,
            ttl_seconds=ttl_seconds
            if ttl_seconds is not None
            else float(settings.demo_seat_ttl_seconds),
        )
    return _registry


def reset_demo_seat_registry_for_tests() -> None:
    """Drop the singleton so the next call rebuilds from settings (tests only)."""
    global _registry
    _registry = None
