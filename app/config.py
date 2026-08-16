"""Application settings.

Everything here is process configuration. User-facing policy lives in
``config/rules.md`` and is loaded by :mod:`app.security`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="JARVIS_",
        extra="ignore",
    )

    #Server Config
    host: str = "127.0.0.1"
    port: int = 8756
    # Refuse non-loopback binds unless this is explicitly enabled.
    allow_non_loopback: bool = False

    # The Tauri shell serves from a custom scheme in release and from Vite in dev.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "http://tauri.localhost",
        ]
    )

    #Data Config
    data_dir: Path = PROJECT_ROOT / "data"
    rules_path: Path = PROJECT_ROOT / "config" / "rules.md"
    profiles_path: Path = PROJECT_ROOT / "config" / "profiles.json"
    model_catalog_path: Path = PROJECT_ROOT / "config" / "model_catalog.json"

    # Opt-in Hugging Face Hub enrichment for model recommendations (default off).
    model_metrics_online: bool = False
    # Optional HF Hub token for higher rate limits; never required.
    hf_token: SecretStr | None = None

    #Ollama Config
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_main: str = "deepseek-r1:8b"
    ollama_fallback: str | None = None
    # How long Ollama keeps models in VRAM after a request (chat / embed /
    # extraction / rerank). Default 30m avoids cold-load spikes after short
    # idle; use "-1" to pin until unload, or "" for Ollama's own default (5m).
    ollama_keep_alive: str = "30m"
    # Voice LLM keep_alive when voice_model ≠ chat_model. Same default; set
    # shorter (e.g. "5m") if you prefer chat to reclaim VRAM after radar use.
    ollama_voice_keep_alive: str = "30m"
    # POST /api/generate with no prompt to load chat (+ embed) at boot.
    ollama_warm_on_boot: bool = False

    #OpenAI Config
    openai_api_key: SecretStr | None = None
    openai_main: str = "gpt-5-mini"
    openai_fallback: str = "gpt-4o-mini"

    # Obsidian Local REST API plugin. Only reachable while Obsidian is running,
    # so every call through it needs a fallback.
    obsidian_rest_url: str = "http://127.0.0.1:27123"
    obsidian_api_key: SecretStr | None = None
    obsidian_verify_tls: bool = False

    # Fast model used by medium/high ingest effort to pick or score chunkers.
    # Profile.chunk_decision_model is what the UI edits; this is the process default.
    chunk_decision_model: str = "qwen3.5:2b"
    chunk_decision_provider: str = "ollama"

    # Postgres + pgvector for the hybrid index. Empty means placeholder engine.
    database_url: str = ""
    # When true and JARVIS_DATABASE_URL is set, refuse to boot on connect/schema
    # failure instead of falling back to PlaceholderRetrievalEngine.
    database_required: bool = False

    # Tracing ships prompt text and retrieved note content to LangSmith's cloud.
    # Off by default: it undercuts the reason for running models locally.
    # Set JARVIS_LANGSMITH_* (not bare LANGSMITH_*) — Settings uses env_prefix.
    langsmith_tracing_v2: bool = False
    langsmith_project: str = "jarvis"
    langsmith_api_key: SecretStr | None = None
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    # Timeout guard for helper LLM calls (mode routing, tag extraction, rerank).
    # Keep short so chat never stalls for minutes before first token.
    llm_aux_timeout_seconds: float = 12.0

    #App Config
    app_env: str="development"
    log_level: str="INFO"
    # Per-IP sliding window (desktop SSE + polls need headroom above 20/minute).
    rate_limit: str = "60/minute"
    # Extra budget when Bearer / X-Jarvis-Token is present (keyed by token hash).
    rate_limit_per_user: str = "120/minute"
    # Process-wide ceiling across all clients.
    rate_limit_global: str = "300/minute"
    cache_ttl_seconds: int=300
    max_retries: int=3
    # Cap JSON body size for chat/voice (bytes). 0 disables the check.
    max_request_body_bytes: int = 65_536
    # Larger budget for Settings document uploads (base64 JSON). 0 disables.
    max_ingest_body_bytes: int = 14_000_000

    # Public NASA Hackathime demo lockdown (forces GPT-4o mini, sample vault).
    demo_mode: bool = False

    # Supabase Auth (demo / hosted). Verify user JWTs via Auth HTTP API.
    supabase_url: str = ""
    supabase_anon_key: SecretStr | None = None

    # Shared secret for local API auth. Protected /api/* routes (all except
    # health) require Bearer or X-Jarvis-Token when a token is configured.
    # When unset, those routes return 401 unless allow_unauthenticated_api.
    # Tauri and scripts/start-web.ps1 mint/inject a session token.
    # In demo mode with Supabase configured, Bearer is a user access token.
    api_token: SecretStr | None = None
    # Lab/pytest only: allow /api/* with no JARVIS_API_TOKEN. Default false.
    allow_unauthenticated_api: bool = False

    # Local Fish Speech TTS (OpenAudio S1-mini). Fish runs separately;
    # Jarvis POSTs to /v1/tts. Demo / missing server → Web Speech fallback.
    tts_enabled: bool = True
    tts_base_url: str = "http://127.0.0.1:8080"
    # Optional Fish reference voice id (references/<id>/ on the Fish server).
    tts_reference_id: str | None = None
    # Fixed inference seed so the untethered default voice stays consistent
    # across replies. Use tts_reference_id when you want a cloned timbre.
    tts_seed: int = 42
    # Lower temperature / top_p → more stable, slightly more natural delivery.
    tts_temperature: float = 0.55
    tts_top_p: float = 0.7
    # Smaller chunks start Fish audio sooner (API range typically 100–300).
    tts_chunk_length: int = 100
    # OpenAudio S1-mini decoder rate; advertised on streamed /api/tts responses.
    tts_sample_rate: int = 44100
    tts_timeout_seconds: float = 60.0
    tts_probe_timeout_seconds: float = 2.0
    # Start Docker Fish Speech on Jarvis boot when the API is down.
    tts_autostart: bool = True
    tts_autostart_timeout_seconds: float = 120.0
    # Force CPU Fish image (skip --gpus). Default prefers CUDA on this machine.
    tts_fish_cpu: bool = False

    @property
    def db_path(self) -> Path:
        """SQLite path under ``data_dir`` for the durable job store."""
        return self.data_dir / "jarvis.db"

    def resolved_api_token(self) -> str | None:
        """Return the configured API token string, or None when unset/blank."""
        if self.api_token is None:
            return None
        value = self.api_token.get_secret_value().strip()
        return value or None

    def is_production(self) -> bool:
        """True when ``app_env`` is the production profile (case-insensitive)."""
        return self.app_env.strip().lower() == "production"

    def resolved_openai_api_key(self) -> str | None:
        """Return the OpenAI API key string, or None when unset/blank.

        Blank values must fail closed so LangChain cannot fall back to an
        ambient ``OPENAI_API_KEY`` via ``api_key=None``.
        """
        if self.openai_api_key is None:
            return None
        value = self.openai_api_key.get_secret_value().strip()
        return value or None

    def resolved_hf_token(self) -> str | None:
        """Return the optional Hugging Face token, or None when unset/blank."""
        if self.hf_token is None:
            return None
        value = self.hf_token.get_secret_value().strip()
        return value or None

    def resolved_supabase_anon_key(self) -> str | None:
        """Return the Supabase anon/publishable key, or None when unset/blank."""
        if self.supabase_anon_key is None:
            return None
        value = self.supabase_anon_key.get_secret_value().strip()
        return value or None


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgres"})


class UnsafeDatabaseUrl(RuntimeError):
    """Database URL failed scheme or loopback policy checks."""


def is_loopback_host(host: str | None) -> bool:
    """True for loopback peer addresses (IPv4, IPv6, and IPv4-mapped IPv6)."""
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in _LOOPBACK_HOSTS:
        return True
    # Starlette may report IPv4-mapped form for dual-stack sockets.
    if normalized.startswith("::ffff:"):
        return normalized.removeprefix("::ffff:") in _LOOPBACK_HOSTS
    return False


def assert_safe_bind(host: str, *, allow_non_loopback: bool) -> None:
    """Raise if ``host`` would expose the API off-loopback without opt-in."""
    if is_loopback_host(host):
        return
    if allow_non_loopback:
        return
    msg = (
        f"Refusing to bind to {host!r}. Jarvis defaults to loopback only. "
        "Set JARVIS_ALLOW_NON_LOOPBACK=true to opt in to a non-loopback host."
    )
    raise RuntimeError(msg)


def normalize_database_url(url: str) -> str:
    """Strip SQLAlchemy-style driver suffixes; return a psycopg-friendly URL."""
    return (
        url.strip()
        .replace("postgresql+psycopg://", "postgresql://")
        .replace("postgres+psycopg://", "postgresql://")
    )


def database_url_log_label(url: str) -> str:
    """Return ``host:port/db`` for logs; never include user or password."""
    from urllib.parse import urlparse

    parsed = urlparse(normalize_database_url(url))
    # Prefer libpq query overrides so logs match where we actually connect.
    targets = _database_url_connect_targets(parsed)
    host = targets[0] if targets else (parsed.hostname or "?")
    port = f":{parsed.port}" if parsed.port else ""
    db = (parsed.path or "/").lstrip("/") or "?"
    return f"{host}{port}/{db}"


def _database_url_connect_targets(parsed: object) -> list[str]:
    """Return host/hostaddr targets libpq would use (query overrides authority).

    Libpq lets ``?host=`` / ``?hostaddr=`` replace the URL authority. Comma-
    separated multi-host lists are split so each candidate is checked.
    """
    from urllib.parse import parse_qs

    query = getattr(parsed, "query", "") or ""
    qs = parse_qs(query)
    targets: list[str] = []
    for key in ("host", "hostaddr"):
        for raw in qs.get(key, []):
            targets.extend(
                part.strip().lower()
                for part in raw.split(",")
                if part.strip()
            )
    if targets:
        return targets
    hostname = getattr(parsed, "hostname", None)
    host = (hostname or "").strip().lower()
    return [host] if host else []


def assert_safe_database_url(url: str, *, allow_non_loopback: bool) -> str:
    """Normalize and validate a Postgres URL; return the normalized form.

    Rejects non-Postgres schemes after normalize. When ``allow_non_loopback``
    is false, requires every connect target to be loopback (``127.0.0.1`` /
    ``localhost`` / ``::1``), matching the API bind posture. Query overrides
    ``host`` / ``hostaddr`` (including multi-host lists) are checked so they
    cannot bypass the authority-host gate.
    """
    from urllib.parse import urlparse

    normalized = normalize_database_url(url)
    if not normalized:
        raise UnsafeDatabaseUrl("JARVIS_DATABASE_URL is empty")

    parsed = urlparse(normalized)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _POSTGRES_SCHEMES:
        raise UnsafeDatabaseUrl(
            f"Refusing database URL scheme {scheme!r}. "
            "Only postgresql:// or postgres:// are allowed."
        )

    targets = _database_url_connect_targets(parsed)
    if not targets:
        raise UnsafeDatabaseUrl("JARVIS_DATABASE_URL is missing a host")
    if allow_non_loopback:
        return normalized
    for host in targets:
        if host not in _LOOPBACK_HOSTS:
            raise UnsafeDatabaseUrl(
                f"Refusing non-loopback database host {host!r}. "
                "Jarvis defaults to loopback only. "
                "Set JARVIS_ALLOW_NON_LOOPBACK=true to opt in."
            )
    return normalized


def assert_token_for_exposure(
    *,
    allow_non_loopback: bool,
    api_token: str | None,
    allow_unauthenticated_api: bool = False,
    supabase_auth: bool = False,
) -> None:
    """Refuse exposed or open binds that lack API token or Supabase Auth."""
    if allow_non_loopback and not api_token and not supabase_auth:
        msg = (
            "JARVIS_ALLOW_NON_LOOPBACK=true requires JARVIS_API_TOKEN "
            "or Supabase Auth (JARVIS_SUPABASE_URL + JARVIS_SUPABASE_ANON_KEY). "
            "Refusing to start with an unauthenticated non-loopback bind."
        )
        raise RuntimeError(msg)
    if allow_unauthenticated_api and allow_non_loopback:
        msg = (
            "JARVIS_ALLOW_UNAUTHENTICATED_API cannot be combined with "
            "JARVIS_ALLOW_NON_LOOPBACK."
        )
        raise RuntimeError(msg)


def parse_rate_limit(spec: str) -> tuple[int, float]:
    """Parse ``N/minute``-style specs into (max_requests, window_seconds)."""
    raw = spec.strip().lower()
    count_str, sep, unit = raw.partition("/")
    if not sep:
        msg = f"Invalid rate_limit {spec!r}; expected like '20/minute'"
        raise ValueError(msg)
    try:
        count = int(count_str.strip())
    except ValueError as exc:
        msg = f"Invalid rate_limit count in {spec!r}"
        raise ValueError(msg) from exc
    if count < 1:
        msg = f"rate_limit count must be >= 1, got {count}"
        raise ValueError(msg)
    windows = {
        "second": 1.0,
        "seconds": 1.0,
        "minute": 60.0,
        "minutes": 60.0,
        "hour": 3600.0,
        "hours": 3600.0,
    }
    window = windows.get(unit.strip())
    if window is None:
        msg = f"Invalid rate_limit unit in {spec!r}"
        raise ValueError(msg)
    return count, window


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once; ensure ``data_dir`` exists as a side effect."""
    settings = Settings()
    if settings.demo_mode:
        # Imported lazily to avoid a circular import at module load.
        from app.demo import apply_demo_settings_overrides

        apply_demo_settings_overrides(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings