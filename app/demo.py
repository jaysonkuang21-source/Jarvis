"""Demo-mode lockdown helpers for the public NASA Hackathime deployment.

When ``JARVIS_DEMO_MODE`` is on, the process forces a single OpenAI chat
model, rejects model/provider profile edits, and scrubs paths from APIs.
Chat uses per-request BYOK (``X-Jarvis-User-LLM-Key``), never a shared
server OpenAI key for user turns. Public demo is login-free; abuse controls
are rate limits plus a small per-IP concurrent seat cap.
"""

from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT, Settings, get_settings
from app.models import Profile, Provider, RagMode

# Locked chat model for the public demo (must match OpenAI project allowlist).
DEMO_CHAT_MODEL = "gpt-4o-mini"
DEMO_CHAT_PROVIDER = Provider.OPENAI
DEMO_EMBED_MODEL = "text-embedding-3-small"
DEMO_EMBED_PROVIDER = Provider.OPENAI

# Profile fields clients must not change in demo mode.
_LOCKED_PROFILE_FIELDS = frozenset(
    {
        "chat_model",
        "chat_provider",
        "voice_model",
        "voice_provider",
        "embedding_model",
        "embedding_provider",
        "chunk_decision_model",
        "chunk_decision_provider",
        "extraction_model",
        "extraction_provider",
        "rerank_model",
        "rerank_provider",
        "rag_mode",
        "model_metrics_online",
        "tracing_enabled",
    }
)

DEMO_VAULT_DIR = PROJECT_ROOT / "demo" / "vault"
DEMO_RULES_PATH = PROJECT_ROOT / "demo" / "rules.md"
DEMO_PROFILES_PATH = PROJECT_ROOT / "demo" / "profiles.json"


def is_demo_mode(settings: Settings | None = None) -> bool:
    """True when the process is running the public demo lockdown."""
    return (settings or get_settings()).demo_mode


def demo_vault_path() -> Path:
    """Absolute path to the shipped sample vault."""
    return DEMO_VAULT_DIR.resolve()


def apply_demo_settings_overrides(settings: Settings) -> None:
    """Mutate settings in place for a safe public demo process.

    Called once at settings load when ``demo_mode`` is true. Forces production
    posture, disables local-only services, and points policy/profile at
    ``demo/`` assets when those files exist.
    """
    settings.app_env = "production"
    settings.tts_enabled = False
    settings.tts_autostart = False
    settings.ollama_warm_on_boot = False
    settings.langsmith_tracing_v2 = False
    settings.model_metrics_online = False
    # Public demo is open (no Supabase login); seats + BYOK + rate limits apply.
    settings.allow_unauthenticated_api = True

    # Tighten defaults unless the operator already set custom values.
    # Demo caps are +50% vs the previous 20/30/200 demo defaults.
    if settings.rate_limit == "60/minute":
        settings.rate_limit = "30/minute"
    if settings.rate_limit_per_user == "120/minute":
        settings.rate_limit_per_user = "45/minute"
    # Global stays at the stock 300/minute default (was 200 in older demos).

    # Always pin demo assets when present so a dashboard JARVIS_RULES_PATH
    # cannot accidentally point at a personal desktop policy file.
    if DEMO_RULES_PATH.is_file():
        settings.rules_path = DEMO_RULES_PATH
    if DEMO_PROFILES_PATH.is_file():
        settings.profiles_path = DEMO_PROFILES_PATH


def force_demo_profile(profile: Profile) -> Profile:
    """Return a copy locked to GPT-4o mini OpenAI with regular RAG."""
    return profile.model_copy(
        update={
            "chat_model": DEMO_CHAT_MODEL,
            "chat_provider": DEMO_CHAT_PROVIDER,
            "voice_model": DEMO_CHAT_MODEL,
            "voice_provider": DEMO_CHAT_PROVIDER,
            "embedding_model": DEMO_EMBED_MODEL,
            "embedding_provider": DEMO_EMBED_PROVIDER,
            "chunk_decision_model": DEMO_CHAT_MODEL,
            "chunk_decision_provider": DEMO_CHAT_PROVIDER,
            "extraction_model": DEMO_CHAT_MODEL,
            "extraction_provider": DEMO_CHAT_PROVIDER,
            "rerank_model": DEMO_CHAT_MODEL,
            "rerank_provider": DEMO_CHAT_PROVIDER,
            "rag_mode": RagMode.REGULAR,
            "agentic_max_iters": 1,
            "tracing_enabled": False,
            "model_metrics_online": False,
        }
    )


def locked_profile_field_changes(before: dict, after: dict) -> list[str]:
    """Return locked field names that differ between two profile dumps."""
    changed: list[str] = []
    for field in sorted(_LOCKED_PROFILE_FIELDS):
        if before.get(field) != after.get(field):
            changed.append(field)
    return changed


def scrub_absolute_path(path: Path | str | None) -> str | None:
    """Return a basename-only label so API JSON never leaks home directories."""
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    return Path(text).name or "vault"
