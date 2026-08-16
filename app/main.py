"""FastAPI application.

Runs as a Tauri sidecar bound to loopback. Streaming uses SSE rather than a
websocket: the traffic is one-directional, reconnect semantics come for free,
and cancelling is just aborting the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.agent import PlaceholderRetrievalEngine, get_registry
from app.cache import (
    clear_answer_caches,
    get_note_cache,
    get_response_cache,
    section_bounds,
)
from app.config import (
    Settings,
    assert_safe_bind,
    assert_safe_database_url,
    assert_token_for_exposure,
    database_url_log_label,
    get_settings,
    is_loopback_host,
    parse_rate_limit,
)
from app import __version__
from app.db import (
    check_postgres_ready,
    close_pool,
    database_configured,
    try_ensure_schema,
)
from app.ingestion.index import run_reindex
from app.models import (
    ApprovalDecision,
    Citation,
    CitationsEvent,
    ChatRequest,
    DoneEvent,
    ErrorEvent,
    ErrorResponse,
    HealthResponse,
    IndexStatus,
    MetricsResponse,
    ModelInfo,
    OptionsResponse,
    Profile,
    ProfileMatrix,
    ProfileValidation,
    Provider,
    RecommendRequest,
    RecommendResponse,
    StreamEvent,
    SystemInfo,
    TokenEvent,
    TtsRequest,
    TtsStatus,
    profile_matrix,
    validate_profile,
)
from app.monitoring import (
    configure_logging,
    diff_mappings,
    get_metrics,
    log_config_change,
    logger,
)
from app.obsidian import get_obsidian_client, obsidian_uri
from app.retrieval import PostgresHybridEngine
from app.scheduler import CreateJobRequest, Job, JobStore, Scheduler
from app.security import (
    ApprovalRequired,
    Policy,
    PolicyDenied,
    activate_turn,
    get_policy_engine,
    policy_elevation_reasons,
    reset_turn,
)


class EventBus:
    """Fan-out for server-initiated events such as a timer firing."""

    def __init__(self) -> None:
        """Create an empty subscriber set for SSE fan-out."""
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        """Register a bounded queue and return it for the events stream."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Drop a subscriber when its SSE connection closes."""
        self._subscribers.discard(queue)

    def publish(self, name: str, payload: dict) -> None:
        """Enqueue an SSE frame for every subscriber; drop slow consumers."""
        message = f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)


bus = EventBus()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start scheduler, optional Fish/Ollama warm, and engine; stop cleanly on shutdown."""
    configure_logging()
    settings = get_settings()
    from app.auth_supabase import supabase_auth_configured
    from app.demo import is_demo_mode, scrub_absolute_path

    assert_safe_bind(settings.host, allow_non_loopback=settings.allow_non_loopback)
    assert_hosted_demo_posture(settings)
    assert_token_for_exposure(
        allow_non_loopback=settings.allow_non_loopback,
        api_token=settings.resolved_api_token(),
        allow_unauthenticated_api=settings.allow_unauthenticated_api,
        supabase_auth=supabase_auth_configured(settings),
        demo_mode=settings.demo_mode,
    )
    if settings.demo_mode:
        logger.info(
            "Demo mode enabled: chat locked to GPT-4o mini; sample vault; "
            "no login; session BYOK required for chat; max %s seats/IP",
            settings.demo_max_seats_per_ip,
        )
    # Reload so tests / env overrides always pick the active rules_path.
    get_policy_engine().reload()
    if settings.allow_unauthenticated_api:
        logger.warning(
            "JARVIS_ALLOW_UNAUTHENTICATED_API is enabled — /api/* is open on "
            "this bind. Use only for local lab/pytest."
        )

    policy = get_policy_engine()
    vault_label = scrub_absolute_path(policy.vault_path) if is_demo_mode(settings) else (
        policy.vault_path or "(unset)"
    )
    logger.info(
        "Policy loaded: delete=%s download=%s shell=%s vault=%s",
        policy.policy.allow_delete,
        policy.policy.allow_download,
        policy.policy.allow_shell,
        vault_label,
    )

    store = JobStore(settings.db_path)
    scheduler = Scheduler(store)

    from app.tools.timers import set_runtime_scheduler

    set_runtime_scheduler(scheduler)

    async def notify(job: Job) -> None:
        """Publish a fired timer/reminder to connected event streams."""
        bus.publish(
            "notification",
            {
                "id": job.id,
                "kind": job.kind.value,
                "title": job.title,
                "body": job.body,
                "missed": job.missed,
                "fire_at": job.fire_at.isoformat(),
            },
        )

    scheduler.subscribe(notify)
    await scheduler.start()

    app.state.scheduler = scheduler
    if database_configured():
        # Scheme / loopback policy always fails loud, even with soft fallback.
        assert_safe_database_url(
            settings.database_url,
            allow_non_loopback=settings.allow_non_loopback,
        )
        if try_ensure_schema():
            app.state.engine = PostgresHybridEngine()
            logger.info("Retrieval engine: postgres-hybrid")
        elif settings.database_required:
            label = database_url_log_label(settings.database_url)
            raise RuntimeError(
                "JARVIS_DATABASE_REQUIRED=true but Postgres connect/schema "
                f"failed (target={label}). Refusing placeholder retrieval."
            )
        else:
            app.state.engine = PlaceholderRetrievalEngine(get_registry())
            logger.info(
                "Retrieval engine: placeholder (Postgres unreachable target=%s)",
                database_url_log_label(settings.database_url),
            )
    else:
        app.state.engine = PlaceholderRetrievalEngine(get_registry())
        logger.info("Retrieval engine: placeholder (no JARVIS_DATABASE_URL)")
    app.state.metrics = get_metrics()
    app.state.reindex_task = None
    # Crash mid-reindex leaves DB indexing=true; clear orphans so UI is usable.
    if database_configured() and try_ensure_schema():
        try:
            from app.db import repo as index_repo

            meta = index_repo.fetch_index_meta()
            if meta.get("indexing"):
                index_repo.set_indexing(False)
                logger.warning(
                    "Cleared orphaned indexing flag left by a previous session"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not clear orphaned indexing flag (%s)", exc)
    if settings.tts_enabled and not settings.demo_mode:
        # Autostart Fish Speech Docker if needed, then probe readiness.
        try:
            from app.tts import warm_voice

            await asyncio.to_thread(warm_voice)
        except Exception:  # noqa: BLE001 — boot continues without TTS
            logger.warning("Fish Speech TTS probe raised", exc_info=True)
    if settings.ollama_warm_on_boot and not settings.demo_mode:
        # Load chat (+ embed) into Ollama so the first turn skips cold VRAM load.
        try:
            from app.ollama_runtime import warm_ollama_from_profile

            await asyncio.to_thread(warm_ollama_from_profile, _load_profile())
        except Exception:  # noqa: BLE001 — boot continues without warm
            logger.warning("Ollama warm-on-boot raised", exc_info=True)
    try:
        yield
    finally:
        from app.tools.timers import set_runtime_scheduler

        set_runtime_scheduler(None)
        await scheduler.stop()
        store.close()
        close_pool()
        logger.info("Shutdown metrics: %s", get_metrics().summary)


app = FastAPI(
    title="Jarvis",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if get_settings().is_production() else "/docs",
    redoc_url=None if get_settings().is_production() else "/redoc",
    openapi_url=None if get_settings().is_production() else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Jarvis-Token",
        "X-Jarvis-User-LLM-Key",
        "X-Jarvis-User-LLM-Base-Url",
        "X-Jarvis-Demo-Seat",
    ],
)


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # microphone=(self): browser STT on this origin; camera/geo stay locked off.
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
}


_RATE_LIMIT_MAX_KEYS = 4096
_GLOBAL_BUCKET_KEY = "global"


class MultiRateLimiter:
    """In-process sliding-window limits: global, per-IP, and per-token hash."""

    def __init__(
        self,
        *,
        ip_max: int,
        ip_window: float,
        user_max: int,
        user_window: float,
        global_max: int,
        global_window: float,
        max_keys: int = _RATE_LIMIT_MAX_KEYS,
    ) -> None:
        """Store budgets and a shared timestamp map capped at ``max_keys``."""
        self._ip_max = ip_max
        self._ip_window = ip_window
        self._user_max = user_max
        self._user_window = user_window
        self._global_max = global_max
        self._global_window = global_window
        self._max_keys = max_keys
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    def _prune(self, key: str, now: float, window: float) -> list[float]:
        """Drop expired timestamps for ``key`` and return the live bucket."""
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = []
            self._hits[key] = bucket
        cutoff = now - window
        del bucket[: next((i for i, t in enumerate(bucket) if t > cutoff), len(bucket))]
        return bucket

    def _evict_idle(self) -> None:
        """Shrink ``_hits`` when over cap by dropping empty then oldest keys."""
        if len(self._hits) <= self._max_keys:
            return
        empty = [
            key
            for key, bucket in self._hits.items()
            if key != _GLOBAL_BUCKET_KEY and not bucket
        ]
        for key in empty:
            del self._hits[key]
            if len(self._hits) <= self._max_keys:
                return
        ranked = sorted(
            (
                (bucket[-1] if bucket else 0.0, key)
                for key, bucket in self._hits.items()
                if key != _GLOBAL_BUCKET_KEY
            ),
            key=lambda item: item[0],
        )
        for _last, key in ranked:
            if len(self._hits) <= self._max_keys:
                break
            self._hits.pop(key, None)

    async def allow(self, request: Request) -> str | None:
        """Consume budgets; skip IP when the request carries a validated identity.

        Loopback exemption is handled by the middleware (full skip). Here, a
        valid first-party or Supabase user token still pays global + per-user
        so a leaked remote token cannot empty the process, but does not burn
        the shared IP bucket used for untrusted clients.
        """
        host = _client_ip(request)
        ip_key = f"ip:{host}"
        presented = _presented_api_token(request)
        expected = get_settings().resolved_api_token()
        user_key: str | None = None
        skip_ip = False
        auth_user = getattr(request.state, "auth_user_id", None)
        if isinstance(auth_user, str) and auth_user:
            user_key = f"user:supabase:{auth_user}"
            skip_ip = True
        elif expected and presented and _tokens_match(presented, expected):
            digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
            user_key = f"user:{digest}"
            skip_ip = True

        now = time.monotonic()
        async with self._lock:
            global_bucket = self._prune(
                _GLOBAL_BUCKET_KEY, now, self._global_window
            )
            if len(global_bucket) >= self._global_max:
                return "global"

            ip_bucket: list[float] | None = None
            if not skip_ip:
                ip_bucket = self._prune(ip_key, now, self._ip_window)
                if len(ip_bucket) >= self._ip_max:
                    return "ip"

            user_bucket: list[float] | None = None
            if user_key is not None:
                user_bucket = self._prune(user_key, now, self._user_window)
                if len(user_bucket) >= self._user_max:
                    return "user"

            global_bucket.append(now)
            if ip_bucket is not None:
                ip_bucket.append(now)
            if user_bucket is not None:
                user_bucket.append(now)
            self._evict_idle()
            return None


def _build_rate_limiter() -> MultiRateLimiter:
    """Construct the multi-scope rate limiter from settings."""
    settings = get_settings()
    ip_max, ip_window = parse_rate_limit(settings.rate_limit)
    user_max, user_window = parse_rate_limit(settings.rate_limit_per_user)
    global_max, global_window = parse_rate_limit(settings.rate_limit_global)
    return MultiRateLimiter(
        ip_max=ip_max,
        ip_window=ip_window,
        user_max=user_max,
        user_window=user_window,
        global_max=global_max,
        global_window=global_window,
    )


_rate_limiter = _build_rate_limiter()
_RATE_LIMIT_EXEMPT = frozenset({"/api/health"})
_AUTH_ALWAYS_OPEN = frozenset({"/api/health"})
_DEMO_SEAT_EXEMPT = frozenset({"/api/health", "/api/demo/seat"})
DEMO_SEAT_HEADER = "X-Jarvis-Demo-Seat"


def _client_ip(request: Request) -> str:
    """Best-effort client IP; prefer first X-Forwarded-For hop in demo/proxy."""
    settings = get_settings()
    if settings.demo_mode or settings.allow_non_loopback:
        forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            # Left-most is the original client when proxies append.
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
    host = request.client.host if request.client else None
    return host or "unknown"


def _presented_api_token(request: Request) -> str:
    """Read Bearer or X-Jarvis-Token from the request; empty when absent."""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Jarvis-Token") or "").strip()


def _tokens_match(presented: str, expected: str) -> bool:
    """Constant-time compare via SHA-256 digests (handles unequal lengths)."""
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def _is_process_api_token(request: Request) -> bool:
    """True when the request authenticates with ``JARVIS_API_TOKEN`` (operators)."""
    expected = get_settings().resolved_api_token()
    if not expected:
        return False
    presented = _presented_api_token(request)
    if not presented:
        return False
    return _tokens_match(presented, expected)


def assert_hosted_demo_posture(settings: Settings) -> None:
    """Refuse production non-loopback binds unless demo lockdown is on."""
    if (
        settings.allow_non_loopback
        and settings.is_production()
        and not settings.demo_mode
    ):
        msg = (
            "Hosted production binds require JARVIS_DEMO_MODE=true. "
            "Personal desktop instances must stay on loopback "
            "(JARVIS_ALLOW_NON_LOOPBACK=false)."
        )
        raise RuntimeError(msg)


def _requires_api_token(request: Request) -> bool:
    """True for every /api/* route when token mode is on, except health."""
    path = request.url.path
    if path in _AUTH_ALWAYS_OPEN:
        return False
    return path.startswith("/api/")


def _rate_limit_detail(scope: str) -> str:
    """Human-readable 429 detail naming the scope that fired (no secrets)."""
    settings = get_settings()
    specs = {
        "ip": settings.rate_limit,
        "user": settings.rate_limit_per_user,
        "global": settings.rate_limit_global,
    }
    return f"Exceeded {scope} rate limit ({specs.get(scope, '?')})"


def _is_loopback_client(request: Request) -> bool:
    """True when the TCP peer is loopback (desktop sidecar / Vite proxy)."""
    host = request.client.host if request.client else None
    return is_loopback_host(host)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach baseline browser security headers to every HTTP response."""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Enforce budgets for untrusted clients; skip first-party local traffic.

    Health stays path-exempt. Loopback peers (Tauri webview, Vite proxy,
    same-machine desktop) skip all rate-limit buckets — IP limits exist for
    remote/untrusted callers, not app-local traffic. Validated identities on
    non-loopback peers still skip the IP bucket inside the limiter. Demo mode
    never exempts loopback so local tests exercise the same budgets.
    """
    if request.url.path in _RATE_LIMIT_EXEMPT:
        get_metrics().record_rate_limit_exemption("path")
        return await call_next(request)
    settings = get_settings()
    if _is_loopback_client(request) and not settings.demo_mode:
        get_metrics().record_rate_limit_exemption("loopback")
        return await call_next(request)
    denied = await _rate_limiter.allow(request)
    if denied is not None:
        get_metrics().record_rate_limit_denial(denied)
        logger.warning(
            "Rate limited (%s) %s %s",
            denied,
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error="rate_limited",
                detail=_rate_limit_detail(denied),
            ).model_dump(),
        )
    return await call_next(request)


@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    """Require a valid API token or Supabase session on protected routes.

    Registered last so it runs before rate limiting and can stash
    ``request.state.auth_user_id`` for per-user budgets. Demo mode is open
    (login-free); operator tokens are still recognized when presented.
    """
    if not _requires_api_token(request):
        return await call_next(request)

    settings = get_settings()
    presented = _presented_api_token(request)
    expected = settings.resolved_api_token()
    if expected and presented and _tokens_match(presented, expected):
        request.state.auth_user_id = f"api-token:{hashlib.sha256(presented.encode()).hexdigest()[:16]}"
        return await call_next(request)

    from app.auth_supabase import supabase_auth_configured, verify_supabase_access_token

    if supabase_auth_configured(settings) and presented:
        user = await verify_supabase_access_token(presented, settings=settings)
        if user is not None:
            request.state.auth_user_id = user.id
            return await call_next(request)

    if settings.demo_mode:
        return await call_next(request)

    if expected or supabase_auth_configured(settings):
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="unauthorized",
                detail="Missing or invalid API token",
            ).model_dump(),
        )

    if settings.allow_unauthenticated_api:
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content=ErrorResponse(
            error="unauthorized",
            detail=(
                "API token required. Set JARVIS_API_TOKEN, configure Supabase "
                "Auth, or set JARVIS_ALLOW_UNAUTHENTICATED_API=true for local "
                "lab use only."
            ),
        ).model_dump(),
    )


@app.middleware("http")
async def demo_seat_middleware(request: Request, call_next):
    """In demo mode, require a leased seat id for protected API routes."""
    settings = get_settings()
    path = request.url.path
    if not settings.demo_mode or path in _DEMO_SEAT_EXEMPT:
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    if _is_process_api_token(request):
        return await call_next(request)

    from app.demo_seats import get_demo_seat_registry

    seat_id = (request.headers.get(DEMO_SEAT_HEADER) or "").strip()
    if not seat_id:
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error="demo_seat_required",
                detail=(
                    "Demo seat required. Call POST /api/demo/seat first "
                    f"(max {settings.demo_max_seats_per_ip} users per IP)."
                ),
            ).model_dump(),
        )

    registry = get_demo_seat_registry()
    result = await registry.claim(_client_ip(request), seat_id)
    if not result.ok:
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error="demo_seat_limit",
                detail=result.detail or "Demo seat limit reached",
            ).model_dump(),
        )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Map FastAPI HTTP errors to the shared :class:`ErrorResponse` shape."""
    body = ErrorResponse(
        error=exc.detail if isinstance(exc.detail, str) else "request_failed",
        detail=None if isinstance(exc.detail, str) else str(exc.detail),
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 422 with structured validation details for bad request bodies."""
    body = ErrorResponse(error="validation_error", detail=str(exc.errors()))
    return JSONResponse(status_code=422, content=body.model_dump())

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse(event: StreamEvent) -> str:
    """Format a typed stream event as one SSE frame."""
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _index_fingerprint() -> str:
    """Stamp response-cache keys with index identity when Postgres is configured."""
    if not database_configured():
        return "placeholder"
    try:
        from app.db import repo

        meta = repo.fetch_index_meta()
        return "|".join(
            [
                str(meta.get("embedding_model") or ""),
                str(meta.get("extraction_model") or ""),
                str(meta.get("last_indexed_at") or ""),
                str(meta.get("ready") or False),
            ]
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def _reject_oversized_body(request: Request, *, limit: int | None = None) -> None:
    """Raise 413 when Content-Length exceeds the configured body budget."""
    settings = get_settings()
    cap = settings.max_request_body_bytes if limit is None else limit
    if cap <= 0:
        return
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError:
        return
    if length > cap:
        raise HTTPException(
            status_code=413,
            detail=f"Request body too large (max {cap} bytes).",
        )


# ---------------------------------------------------------------------------
# Health and options
# ---------------------------------------------------------------------------


@app.post("/api/demo/seat")
async def claim_demo_seat(request: Request) -> dict[str, object]:
    """Lease or refresh an anonymous demo seat for this client IP.

    Returns a seat id the client must send as ``X-Jarvis-Demo-Seat`` on later
    API calls. At most ``demo_max_seats_per_ip`` concurrent seats per IP.
    """
    settings = get_settings()
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not found")

    from app.demo_seats import get_demo_seat_registry

    existing = (request.headers.get(DEMO_SEAT_HEADER) or "").strip() or None
    result = await get_demo_seat_registry().claim(_client_ip(request), existing)
    if not result.ok or not result.seat_id:
        raise HTTPException(
            status_code=429,
            detail=result.detail or "Demo seat limit reached",
        )
    return {
        "seat_id": result.seat_id,
        "seats_used": result.seats_used,
        "seats_max": result.seats_max,
        "ttl_seconds": settings.demo_seat_ttl_seconds,
    }


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness plus cheap dependency checks (Obsidian, vault, Postgres).

    Overall ``ok``/``healthy`` reflects core readiness (process up + vault;
    Postgres when required). Obsidian plugin stays a check field only.
    In demo mode Obsidian/Fish checks are omitted so no local paths leak.
    """
    settings = get_settings()
    pg_configured = database_configured()
    postgres_ready = False
    if pg_configured:
        postgres_ready = await asyncio.to_thread(check_postgres_ready)

    if settings.demo_mode:
        checks = {
            "demo_mode": True,
            "vault_configured": get_policy_engine().vault_path is not None,
            "postgres_configured": pg_configured,
            "postgres_ready": postgres_ready,
            "chat_model": "gpt-4o-mini",
        }
        healthy = bool(checks["vault_configured"])
        if settings.database_required and pg_configured:
            healthy = healthy and bool(checks["postgres_ready"])
        return HealthResponse(
            status="healthy" if healthy else "degraded",
            environment="demo",
            version=__version__,
            ok=healthy,
            checks=checks,
        )

    obsidian = get_obsidian_client()
    from app.tts import status as tts_status

    fish = tts_status()
    checks = {
        "obsidian_plugin": await obsidian.available(),
        "vault_configured": get_policy_engine().vault_path is not None,
        "postgres_configured": pg_configured,
        "postgres_ready": postgres_ready,
        # Informational only — spoken replies fall back to Web Speech.
        "fish_tts": fish.enabled and fish.ready,
    }
    # Unset Postgres is normal for lab (placeholder engine); do not degrade
    # solely for that. Required+configured readiness can flip overall status.
    # Obsidian Local REST is optional UX — never gate overall health on it.
    healthy = bool(checks["vault_configured"])
    if settings.database_required and pg_configured:
        healthy = healthy and bool(checks["postgres_ready"])
    return HealthResponse(
        status="healthy" if healthy else "degraded",
        environment=settings.app_env,
        version=__version__,
        ok=healthy,
        checks=checks,
    )


@app.get("/api/metrics", response_model=MetricsResponse)
async def metrics() -> MetricsResponse:
    """Process-lifetime request counters and derived rates."""
    return get_metrics().to_response()


@app.get("/api/options", response_model=OptionsResponse)
async def options(refresh: bool = False) -> OptionsResponse:
    """Catalog of models and enum choices for the settings UI."""
    models = await get_registry().all(force=refresh)
    values = list(models.values())
    return OptionsResponse(
        chat_models=[m for m in values if not m.is_embedding],
        embedding_models=[m for m in values if m.is_embedding],
        query_modes=[
            {"value": "local", "label": "Local",
             "hint": "Hybrid retrieve, then walk entity neighbourhoods. Best for specific facts."},
            {"value": "global", "label": "Global",
             "hint": "Map-reduce over community summaries. Best for themes across the vault."},
            {"value": "drift", "label": "DRIFT",
             "hint": "Probe broadly, then drill into promising regions."},
            {"value": "auto", "label": "Auto",
             "hint": "A fast model chooses Local, Global, or DRIFT for you."},
        ],
        rag_modes=[
            {"value": "regular", "label": "Regular", "hint": "One retrieval pass, then answer."},
            {"value": "agentic", "label": "Agentic",
             "hint": "Grade relevance and rewrite the query until docs are useful or the limit hits."},
        ],
        chunkers=[
            {"value": "recursive", "label": "Recursive",
             "hint": "Split on headings, then by token budget with overlap."},
            {"value": "semantic", "label": "Semantic",
             "hint": "Embedding-based boundaries. Costs an embedding call per sentence."},
            {"value": "structure_entity", "label": "Structure + links",
             "hint": "Headings and wikilink-aware splits. Matches the Obsidian graph."},
            {"value": "claim_centered", "label": "Claim-centered",
             "hint": "Claim-sized units. Falls back until the LLM claim path ships."},
        ],
        ingest_modes=[
            {"value": "regular", "label": "Text", "hint": "Markdown and extracted document text."},
            {"value": "multimodal", "label": "Visual (ColPali)",
             "hint": "Page images with late interaction. Needs a GPU and a vision model."},
        ],
        ingest_efforts=[
            {"value": "manual", "label": "Manual",
             "hint": "Pick the chunker yourself."},
            {"value": "low", "label": "Low",
             "hint": "Structure + wikilink-aware chunking. No decision model."},
            {"value": "medium", "label": "Medium",
             "hint": "A fast LLM picks among structure, claim, semantic, and recursive."},
            {"value": "high", "label": "High",
             "hint": "Try several chunkers and score connectivity, context loss, and metadata."},
        ],
    )


@app.get("/api/system", response_model=SystemInfo)
async def system_info() -> SystemInfo:
    """Return local RAM/CPU/GPU probe results for Settings and recommendations.

    Demo mode returns an empty probe so host hardware is never exposed.
    """
    if get_settings().demo_mode:
        return SystemInfo(
            ram_total_mb=None,
            ram_available_mb=None,
            cpu_cores=None,
            gpus=[],
            probe_errors=["demo_mode: hardware probe disabled"],
        )
    from app.system.hardware import probe_system

    return probe_system()


@app.post("/api/models/recommend", response_model=RecommendResponse)
async def recommend_models_endpoint(body: RecommendRequest) -> RecommendResponse:
    """Rank models for profile roles using catalog, hardware, and optional HF metrics.

    Does not write the profile; the UI applies picks into the draft like a manual select.
    """
    if get_settings().demo_mode:
        raise HTTPException(
            403,
            "Model recommendations are disabled in demo mode (GPT-4o mini only).",
        )

    from app.models.recommend import recommend_models

    if body.apply:
        raise HTTPException(
            400,
            "Recommend does not write the profile; set apply=false and patch the draft in the UI.",
        )

    active = body.profile or _load_profile()
    settings = get_settings()
    online = body.online
    if online is None:
        online = bool(active.model_metrics_online or settings.model_metrics_online)
    body = body.model_copy(update={"online": online})
    models = await get_registry().all()
    return await recommend_models(body, models=models, profile=active)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def _default_profile() -> Profile:
    """Build a Profile using process defaults from settings."""
    settings = get_settings()
    profile = Profile(
        chunk_decision_model=settings.chunk_decision_model,
        chunk_decision_provider=Provider(settings.chunk_decision_provider),
    )
    if settings.demo_mode:
        from app.demo import force_demo_profile

        return force_demo_profile(profile)
    return profile


def _load_profile() -> Profile:
    """Read the saved profile from disk, or defaults if missing/corrupt."""
    path = get_settings().profiles_path
    if not path.exists():
        return _default_profile()
    try:
        profile = Profile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s (%s); using defaults", path.name, exc)
        return _default_profile()
    if get_settings().demo_mode:
        from app.demo import force_demo_profile

        return force_demo_profile(profile)
    return profile


@app.get("/api/profile", response_model=Profile)
async def get_profile() -> Profile:
    """Return the persisted user profile (demo-forced when lockdown is on)."""
    return _load_profile()


@app.put("/api/profile", response_model=Profile)
async def put_profile(profile: Profile) -> Profile:
    """Write the profile and audit any field diffs.

    Demo mode allows ingest/chunker settings but rejects locked model fields
    and always re-applies ``force_demo_profile`` before persist.
    """
    settings = get_settings()
    previous = _load_profile()
    if settings.demo_mode:
        from app.demo import force_demo_profile, locked_profile_field_changes

        attempted = locked_profile_field_changes(
            previous.model_dump(mode="json"),
            profile.model_dump(mode="json"),
        )
        if attempted:
            raise HTTPException(
                403,
                "Cannot edit locked demo fields: " + ", ".join(attempted),
            )
        profile = force_demo_profile(profile)

    before = previous.model_dump(mode="json")
    after = profile.model_dump(mode="json")

    changed = diff_mappings(before, after)

    path = settings.profiles_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    if changed:
        log_config_change(
            "profile",
            before={k: before[k] for k in changed},
            after={k: after[k] for k in changed},
            changed=changed,
        )
    clear_answer_caches()
    return profile


@app.post("/api/profile/validate", response_model=ProfileValidation)
async def post_validate(profile: Profile) -> ProfileValidation:
    """Check a candidate profile against discovered model availability."""
    return validate_profile(profile, await get_registry().all())


@app.post("/api/profile/matrix", response_model=ProfileMatrix)
async def post_matrix(profile: Profile) -> ProfileMatrix:
    """Single source of truth for which options the settings screen disables."""
    return profile_matrix(profile, await get_registry().all())


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class RulesPayload(BaseModel):
    """Rules update body; elevation requires an explicit confirmation flag."""

    policy: Policy
    confirm_elevation: bool = False


@app.get("/api/rules", response_model=Policy)
async def get_rules() -> Policy:
    """Return the in-memory policy loaded from ``rules.md``.

    Demo mode always reports the fixed sample vault path ``demo/vault``.
    """
    policy = get_policy_engine().policy
    if get_settings().demo_mode:
        return policy.model_copy(update={"vault_path": "demo/vault"})
    return policy


@app.put("/api/rules", response_model=Policy)
async def put_rules(payload: RulesPayload) -> Policy:
    """Persist policy to disk, reload the engine, and audit frontmatter diffs.

    Privilege elevations (enabling shell/download/delete/email, expanding
    sandboxes, adding high-risk tools, or changing vault_path) require
    ``confirm_elevation=true`` or the request is rejected with 403.
    """
    if get_settings().demo_mode:
        raise HTTPException(403, "Rules cannot be edited in demo mode.")

    previous = get_policy_engine().policy
    elevations = policy_elevation_reasons(previous, payload.policy)
    if elevations and not payload.confirm_elevation:
        raise HTTPException(
            status_code=403,
            detail=(
                "Policy elevation requires confirm_elevation=true. "
                f"Changes: {'; '.join(elevations)}"
            ),
        )

    before = previous.model_dump(mode="json", exclude={"prompt_text"})
    after = payload.policy.model_dump(mode="json", exclude={"prompt_text"})
    changed = diff_mappings(before, after)

    payload.policy.dump()
    reloaded = get_policy_engine().reload()

    if changed:
        log_config_change(
            "rules",
            before={k: before[k] for k in changed},
            after={k: after[k] for k in changed},
            changed=changed,
        )
    # Prompt-body edits are still a config change even when frontmatter is identical.
    elif previous.prompt_text != payload.policy.prompt_text:
        log_config_change(
            "rules",
            changed={"prompt_text": {"from": "(redacted)", "to": "(redacted)"}},
        )
    clear_answer_caches()
    return reloaded


# ---------------------------------------------------------------------------
# Index and notes
# ---------------------------------------------------------------------------


@app.get("/api/index/status", response_model=IndexStatus)
async def index_status(request: Request) -> IndexStatus:
    """Index readiness and vault stats from the active retrieval engine."""
    return await request.app.state.engine.index_status()


@app.post("/api/index/reindex", response_model=IndexStatus)
async def reindex(request: Request, force: bool = False) -> IndexStatus:
    """Start a background vault reindex into Postgres.

    Pass ``force=true`` to cancel an in-process job and clear a stuck flag.
    """
    settings = get_settings()
    if not database_configured():
        raise HTTPException(400, "Set JARVIS_DATABASE_URL before reindexing.")
    profile = _load_profile()
    if not profile.extraction_model:
        raise HTTPException(400, "Set extraction_model in the profile before reindexing.")
    engine = request.app.state.engine
    task = getattr(request.app.state, "reindex_task", None)
    if task is not None and not task.done():
        if not force:
            raise HTTPException(
                409,
                "A reindex is already running. Pass force=true to cancel and restart.",
            )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Cancelled reindex task raised")
        request.app.state.reindex_task = None

    # Mark live *before* spawn. Setting True after create_task races a fast
    # job's finally(False) and can leave DB indexing=true with no task.
    if hasattr(engine, "set_indexing"):
        engine.set_indexing(True)

    async def _job() -> None:
        """Run reindex, clear answer caches on success, log failures."""
        try:
            await run_reindex(profile, engine=engine)
            clear_answer_caches()
        except asyncio.CancelledError:
            if hasattr(engine, "set_indexing"):
                engine.set_indexing(False)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Reindex failed")
            if hasattr(engine, "set_indexing"):
                engine.set_indexing(False)

    request.app.state.reindex_task = asyncio.create_task(_job())
    return await engine.index_status()


class NoteResponse(BaseModel):
    path: str
    title: str
    content: str
    section_start: int
    section_end: int


@app.get("/api/notes", response_model=NoteResponse)
async def read_note(path: str, char_start: int = 0) -> NoteResponse:
    """Read a note for the citation preview, expanded to its heading section."""
    policy = get_policy_engine()
    vault = policy.vault_path
    if vault is None:
        raise HTTPException(400, "No vault configured. Set vault_path in config/rules.md.")

    target = (vault / path).resolve()
    verdict = policy.check("vault_read", path=target, mode="read")
    if not verdict.allowed:
        raise HTTPException(403, verdict.reason)
    if not target.is_file():
        raise HTTPException(404, f"{path} not found")

    text = get_note_cache().read(target)
    start, end = section_bounds(text, char_start)
    return NoteResponse(
        path=path,
        title=target.stem,
        content=text,
        section_start=start,
        section_end=end,
    )


class OpenNoteRequest(BaseModel):
    path: str


class OpenNoteResponse(BaseModel):
    opened: bool
    uri: str


class IngestNoteRequest(BaseModel):
    """Paste a single note into the vault Inbox for digestion."""

    content: str = Field(min_length=1)
    title: str = ""
    filename: str | None = None


class IngestUploadFile(BaseModel):
    """One uploaded document (UTF-8 text and/or base64 bytes)."""

    filename: str
    content: str | None = None
    content_base64: str | None = None
    mime: str | None = None


class IngestUploadRequest(BaseModel):
    """Batch of documents to write into the vault Inbox."""

    files: list[IngestUploadFile] = Field(min_length=1)


class IngestedDocument(BaseModel):
    """One written note (and optional binary) with chosen retriever."""

    note_path: str
    file_path: str | None = None
    kind: str
    retriever: str
    tags: list[str] = Field(default_factory=list)


class IngestNotesResponse(BaseModel):
    """Paths written under the vault (Inbox/)."""

    paths: list[str]
    count: int
    documents: list[IngestedDocument] = Field(default_factory=list)


@app.post("/api/notes/open", response_model=OpenNoteResponse)
async def open_note(payload: OpenNoteRequest) -> OpenNoteResponse:
    """Try the Obsidian REST open call; always return a deep-link URI fallback."""
    policy = get_policy_engine()
    vault = policy.vault_path
    if vault is None:
        raise HTTPException(400, "No vault configured. Set vault_path in config/rules.md.")

    target = (vault / payload.path).resolve()
    verdict = policy.check("note_open", path=target, mode="read")
    if not verdict.allowed:
        raise HTTPException(403, verdict.reason)

    vault_name = vault.name
    opened = await get_obsidian_client().open_note(payload.path)
    return OpenNoteResponse(opened=opened, uri=obsidian_uri(vault_name, payload.path))


@app.post("/api/notes/ingest", response_model=IngestNotesResponse)
async def ingest_note_paste(request: Request, payload: IngestNoteRequest) -> IngestNotesResponse:
    """Write a pasted note into ``Inbox/`` under the configured vault."""
    settings = get_settings()
    _reject_oversized_body(request, limit=settings.max_ingest_body_bytes)

    from app.ingestion.formats import DocumentKind, RetrieverKind, kind_tag, retriever_tag
    from app.ingestion.inbox import InboxError, write_inbox_note

    policy = get_policy_engine()
    stem = (payload.filename or payload.title or "note").strip() or "note"
    body = payload.content
    title = (payload.title or "").strip()
    if title and not body.lstrip().startswith("#"):
        body = f"# {title}\n\n{body}"
    tags = [
        kind_tag(DocumentKind.TEXT),
        retriever_tag(RetrieverKind.TEXT_HYBRID),
    ]
    try:
        path = write_inbox_note(policy, filename=stem, content=body, tags=tags)
    except InboxError as exc:
        raise HTTPException(400, str(exc)) from exc
    doc = IngestedDocument(
        note_path=path,
        kind=DocumentKind.TEXT.value,
        retriever=RetrieverKind.TEXT_HYBRID.value,
        tags=tags,
    )
    return IngestNotesResponse(paths=[path], count=1, documents=[doc])


@app.post("/api/notes/ingest/upload", response_model=IngestNotesResponse)
async def ingest_note_upload(
    request: Request, payload: IngestUploadRequest
) -> IngestNotesResponse:
    """Accept any file type; convert to Inbox notes and pick a retriever tag."""
    settings = get_settings()
    _reject_oversized_body(request, limit=settings.max_ingest_body_bytes)

    from app.ingestion.inbox import (
        InboxError,
        decode_data_url_or_base64,
        ingest_upload_bytes,
    )

    policy = get_policy_engine()
    written: list[str] = []
    documents: list[IngestedDocument] = []
    for item in payload.files:
        name = item.filename or "file"
        try:
            if item.content_base64:
                data = decode_data_url_or_base64(item.content_base64)
            elif item.content is not None:
                data = item.content.encode("utf-8")
            else:
                raise InboxError(f"{name} has no content.")
            result = ingest_upload_bytes(
                policy, filename=name, data=data, mime=item.mime
            )
        except InboxError as exc:
            raise HTTPException(400, str(exc)) from exc
        note_path = str(result["note_path"])
        written.append(note_path)
        documents.append(
            IngestedDocument(
                note_path=note_path,
                file_path=str(result["file_path"]) if result.get("file_path") else None,
                kind=str(result["kind"]),
                retriever=str(result["retriever"]),
                tags=list(result.get("tags") or []),
            )
        )
    return IngestNotesResponse(paths=written, count=len(written), documents=documents)


class DocumentChunkPreview(BaseModel):
    """One indexed chunk for the Settings chunk inspector."""

    chunk_id: str
    text: str
    heading_path: list[str] = Field(default_factory=list)
    char_start: int = 0
    char_end: int = 0
    note_path: str = ""
    note_title: str = ""
    tags: list[str] = Field(default_factory=list)
    wikilinks: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)


class IndexedDocumentSummary(BaseModel):
    """One indexed vault path for the cross-session chunk browser."""

    path: str
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    chunk_count: int = 0


class IndexedDocumentsResponse(BaseModel):
    """Inventory of documents currently in the Postgres index."""

    documents: list[IndexedDocumentSummary] = Field(default_factory=list)
    total: int = 0


class DocumentChunksResponse(BaseModel):
    """Chunk inventory for one vault path after reindex."""

    path: str
    total: int
    chunks: list[DocumentChunkPreview] = Field(default_factory=list)


@app.get("/api/index/documents", response_model=IndexedDocumentsResponse)
async def list_indexed_documents() -> IndexedDocumentsResponse:
    """List every indexed document so Settings can inspect chunks across sessions."""
    if not database_configured():
        raise HTTPException(400, "Set JARVIS_DATABASE_URL before listing documents.")

    from app.db import repo

    rows = await asyncio.to_thread(repo.list_indexed_documents)
    documents = [IndexedDocumentSummary(**row) for row in rows]
    return IndexedDocumentsResponse(documents=documents, total=len(documents))


@app.get("/api/index/documents/chunks", response_model=DocumentChunksResponse)
async def list_document_chunks(
    path: str,
    limit: int = 500,
) -> DocumentChunksResponse:
    """List indexed chunks for a vault-relative path (desktop chunk inspector).

    ``total`` is always the full count; ``chunks`` may be capped by ``limit``.
    """
    if not database_configured():
        raise HTTPException(400, "Set JARVIS_DATABASE_URL before listing chunks.")
    trimmed = path.strip().replace("\\", "/")
    if not trimmed:
        raise HTTPException(400, "path is required")
    cap = max(1, min(int(limit), 500))

    from app.db import repo

    total, rows = await asyncio.to_thread(repo.list_chunks_for_path, trimmed, cap)
    return DocumentChunksResponse(
        path=trimmed,
        total=total,
        chunks=[DocumentChunkPreview(**row) for row in rows],
    )


class DeleteDocumentRequest(BaseModel):
    """Remove one indexed vault note from Postgres (and optionally the vault files)."""

    path: str = Field(min_length=1)
    delete_vault_files: bool = False


class DeleteDocumentResponse(BaseModel):
    """Outcome of removing a document from the search index."""

    removed_from_index: bool
    vault_files_trashed: list[str] = Field(default_factory=list)


@app.delete("/api/index/documents", response_model=DeleteDocumentResponse)
async def delete_indexed_document(payload: DeleteDocumentRequest) -> DeleteDocumentResponse:
    """Drop a document from the Postgres index; optionally trash vault files."""
    from app.ingestion.remove import RemoveDocumentError, remove_indexed_document

    policy = get_policy_engine()
    # Demo: drop index rows; keep vault files so the shared sample set stays intact
    # unless the note lives under Inbox (user uploads).
    delete_vault = payload.delete_vault_files
    if get_settings().demo_mode:
        path = (payload.path or "").replace("\\", "/")
        delete_vault = path.startswith("Inbox/") and payload.delete_vault_files

    try:
        result = remove_indexed_document(
            policy,
            path=payload.path,
            delete_vault_files=delete_vault,
        )
    except RemoveDocumentError as exc:
        raise HTTPException(400, str(exc)) from exc
    return DeleteDocumentResponse(
        removed_from_index=bool(result["removed_from_index"]),
        vault_files_trashed=list(result.get("vault_files_trashed") or []),
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(request: Request, payload: ChatRequest) -> StreamingResponse:
    """SSE chat stream; stops early when the client disconnects.

    Demo mode requires a per-request OpenAI-compatible key
    (``X-Jarvis-User-LLM-Key``); the process env key is never used for chat.
    """
    _reject_oversized_body(request)
    settings = get_settings()
    user_llm_key: str | None = None
    user_llm_base: str | None = None
    if settings.demo_mode:
        from app.demo import force_demo_profile
        from app.llm_session import parse_user_llm_headers

        user_llm_key, user_llm_base = parse_user_llm_headers(request.headers)
        if not user_llm_key:
            raise HTTPException(
                400,
                "Demo chat requires a session OpenAI-compatible API key. "
                "Paste it in the demo UI; it is wiped on sign-out. "
                "Always rotate the key after use.",
            )

    engine = request.app.state.engine
    policy = get_policy_engine()
    # Per-request TurnState carries budget + optional grant id; never mint or
    # forge grants from a client-supplied id that was not previously approved.
    turn = policy.begin_turn(payload.approval_id)

    profile = payload.profile
    if settings.demo_mode:
        profile = force_demo_profile(profile)
        payload = payload.model_copy(update={"profile": profile})

    validation = validate_profile(payload.profile, await get_registry().all())
    if not validation.valid:
        reason = "; ".join(i.message for i in validation.issues if i.level == "error")

        async def rejected() -> AsyncIterator[str]:
            """Emit a non-recoverable profile error and close the stream."""
            yield sse(ErrorEvent(message=reason, code="invalid_profile", recoverable=False))
            yield sse(DoneEvent(message_id=uuid.uuid4().hex))

        return StreamingResponse(rejected(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def stream() -> AsyncIterator[str]:
        """Relay engine events, map policy exceptions, and record latency.

        Response cache is bypassed when ``history`` is non-empty so a follow-up
        cannot receive another thread's cached answer. Hits replay citations
        then tokens then done. Sets only store clean, completed successes.
        Demo mode never uses the shared answer cache (BYOK / no cross-user).
        """
        from app.llm_session import request_llm_credentials

        had_error = False
        cache_hit: bool | None = None
        started = time.perf_counter()
        response_cache = get_response_cache()
        index_fp = _index_fingerprint()
        use_cache = not payload.history and not settings.demo_mode
        turn_token = activate_turn(turn)
        try:
            with request_llm_credentials(user_llm_key, user_llm_base):
                if use_cache:
                    cached = response_cache.get(
                        payload.message, payload.profile, index_fingerprint=index_fp
                    )
                    if cached is not None:
                        cache_hit = True
                        if cached["citations"]:
                            citations = [
                                Citation.model_validate(c) for c in cached["citations"]
                            ]
                            yield sse(CitationsEvent(citations=citations))
                        yield sse(TokenEvent(text=cached["response"]))
                        yield sse(
                            DoneEvent(
                                message_id=uuid.uuid4().hex,
                                elapsed_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )
                        return

                cache_hit = False if use_cache else None
                assembled: list[str] = []
                citations_acc: list[dict] = []
                disconnected = False
                approval_required = False
                saw_done = False
                async for event in engine.query(
                    payload.message, payload.profile, payload.history
                ):
                    etype = getattr(event, "type", None)
                    if etype == "error":
                        had_error = True
                    elif etype == "token":
                        assembled.append(getattr(event, "text", "") or "")
                    elif etype == "citations":
                        raw = getattr(event, "citations", None) or []
                        citations_acc = [
                            c.model_dump() if hasattr(c, "model_dump") else dict(c)
                            for c in raw
                        ]
                    elif etype == "approval_required":
                        approval_required = True
                    elif etype == "done":
                        saw_done = True
                    if await request.is_disconnected():
                        disconnected = True
                        logger.info(
                            "Client disconnected; cancelling run",
                            extra={"event": "chat_cancelled"},
                        )
                        break
                    yield sse(event)

                if (
                    use_cache
                    and not had_error
                    and not disconnected
                    and not approval_required
                    and saw_done
                    and assembled
                ):
                    response_cache.set(
                        payload.message,
                        "".join(assembled),
                        payload.profile,
                        index_fingerprint=index_fp,
                        citations=citations_acc,
                    )
        except asyncio.CancelledError:
            raise
        except PolicyDenied as exc:
            had_error = True
            logger.warning(
                "Policy denied tool call: %s",
                exc.verdict.reason,
                extra={
                    "event": "policy_denied",
                    "code": exc.verdict.code,
                    "tool": exc.verdict.tool,
                },
            )
            yield sse(ErrorEvent(message=exc.verdict.reason, code=exc.verdict.code))
            yield sse(DoneEvent(message_id=uuid.uuid4().hex))
        except ApprovalRequired as exc:
            from app.models import ApprovalRequiredEvent

            details = exc.verdict.details or {}
            request_id = policy.mint_approval(
                exc.verdict.tool,
                path=details.get("path"),
                mode=details.get("mode"),
            )
            logger.info(
                "Approval required for %s",
                exc.verdict.tool,
                extra={"event": "approval_required", "tool": exc.verdict.tool},
            )
            yield sse(ApprovalRequiredEvent(
                id=request_id,
                tool=exc.verdict.tool,
                reason=exc.verdict.reason,
                details=details,
            ))
            yield sse(DoneEvent(message_id=uuid.uuid4().hex))
        finally:
            reset_turn(turn_token)
            get_metrics().record_request(
                (time.perf_counter() - started) * 1000,
                error=had_error,
                cache_hit=cache_hit,
            )

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/tts", response_model=TtsStatus)
async def tts_status_endpoint() -> TtsStatus:
    """Report whether local Fish Speech TTS is enabled and reachable."""
    from app.tts import status as tts_status

    return tts_status()


@app.post("/api/tts")
async def tts_synthesize(payload: TtsRequest) -> StreamingResponse:
    """Stream Fish Speech PCM as soon as the first audio segment exists.

    Fish ``streaming=true`` returns raw PCM s16le mono chunks (see ``app.tts``).
    Headers advertise encoding so the UI can play via Web Audio without waiting
    for a full WAV. The frontend falls back to Web Speech on error.
    """
    from app.tts import aiter_synthesize_pcm, strip_for_speech

    settings = get_settings()
    if settings.demo_mode or not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="TTS is disabled")
    if not strip_for_speech(payload.text):
        raise HTTPException(
            status_code=400,
            detail="Nothing to speak after stripping markup",
        )

    pcm = aiter_synthesize_pcm(payload.text)

    try:
        first = await anext(pcm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StopAsyncIteration as exc:
        raise HTTPException(
            status_code=503, detail="Fish Speech returned empty audio"
        ) from exc
    except Exception as exc:
        logger.exception("Fish Speech TTS failed")
        raise HTTPException(
            status_code=503, detail=f"TTS unavailable: {exc}"
        ) from exc

    async def audio_chunks() -> AsyncIterator[bytes]:
        """Yield the peeked PCM chunk, then the rest of the Fish stream."""
        yield first
        try:
            async for chunk in pcm:
                yield chunk
        except Exception:
            logger.exception("Fish Speech TTS stream aborted")

    return StreamingResponse(
        audio_chunks(),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Jarvis-Audio-Encoding": "pcm_s16le",
            "X-Jarvis-Audio-Sample-Rate": str(settings.tts_sample_rate),
            "X-Jarvis-Audio-Channels": "1",
        },
    )


@app.post("/api/voice")
async def voice(request: Request, payload: ChatRequest) -> StreamingResponse:
    """SSE voice agent: direct LLM reply; may call vault_search and timer tools.

    Skips the RAG chat path and does not require an index for ordinary talk.
    Disabled in demo mode (chat-only lockdown).
    """
    _reject_oversized_body(request)
    if get_settings().demo_mode:
        raise HTTPException(403, "Voice is disabled in demo mode.")

    validation = validate_profile(payload.profile, await get_registry().all())
    if not validation.valid:
        reason = "; ".join(i.message for i in validation.issues if i.level == "error")

        async def rejected() -> AsyncIterator[str]:
            """Emit a non-recoverable profile error and close the stream."""
            yield sse(ErrorEvent(message=reason, code="invalid_profile", recoverable=False))
            yield sse(DoneEvent(message_id=uuid.uuid4().hex))

        return StreamingResponse(rejected(), media_type="text/event-stream", headers=SSE_HEADERS)

    from app.voice import stream_voice

    async def stream() -> AsyncIterator[str]:
        """Relay voice agent events until disconnect or completion."""
        started = time.perf_counter()
        had_error = False
        try:
            async for event in stream_voice(
                payload.message, payload.profile, payload.history
            ):
                if getattr(event, "type", None) == "error":
                    had_error = True
                if await request.is_disconnected():
                    break
                yield sse(event)
        finally:
            get_metrics().record_request(
                (time.perf_counter() - started) * 1000,
                error=had_error,
                cache_hit=None,
            )

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/approvals")
async def approve(decision: ApprovalDecision) -> dict:
    """Grant or deny a one-shot tool approval minted by an earlier chat turn."""
    resolved = get_policy_engine().resolve_approval(
        decision.request_id,
        approved=decision.approved,
        tool=decision.tool,
    )
    if not resolved:
        raise HTTPException(404, "Unknown or already resolved approval request")
    logger.info(
        "Approval %s for %s",
        "granted" if decision.approved else "denied",
        decision.tool,
        extra={
            "event": "approval_decision",
            "approved": decision.approved,
            "tool": decision.tool,
            "request_id": decision.request_id,
        },
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


@app.get("/api/timers", response_model=list[Job])
async def list_timers(request: Request) -> list[Job]:
    """List pending scheduled jobs in fire-time order."""
    if get_settings().demo_mode:
        raise HTTPException(403, "Timers are disabled in demo mode.")
    return await request.app.state.scheduler.store.pending()


@app.post("/api/timers", response_model=Job)
async def create_timer(request: Request, payload: CreateJobRequest) -> Job:
    """Schedule a timer, reminder, or delayed email job."""
    if get_settings().demo_mode:
        raise HTTPException(403, "Timers are disabled in demo mode.")
    try:
        return await request.app.state.scheduler.schedule(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/timers/{job_id}")
async def cancel_timer(request: Request, job_id: str) -> dict:
    """Cancel a pending job so it will not fire."""
    if get_settings().demo_mode:
        raise HTTPException(403, "Timers are disabled in demo mode.")
    await request.app.state.scheduler.cancel(job_id)
    return {"ok": True}


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """Server-initiated events. Tauri turns these into native notifications."""
    queue = bus.subscribe()

    async def stream() -> AsyncIterator[str]:
        """Push bus messages with keepalives until the client disconnects."""
        try:
            yield "event: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


def run() -> None:
    """Entry point for ``python -m app.main``: serve with uvicorn."""
    import uvicorn

    settings = get_settings()
    assert_safe_bind(settings.host, allow_non_loopback=settings.allow_non_loopback)
    assert_hosted_demo_posture(settings)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
