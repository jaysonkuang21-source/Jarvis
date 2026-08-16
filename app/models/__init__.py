"""The engine-agnostic contract shared by the backend and the UI.

These types are the only thing the frontend knows about retrieval. Swapping
LightRAG for Microsoft GraphRAG or a Neo4j pipeline means implementing
:class:`RetrievalEngine` and changing nothing else.

``scripts/generate_types.py`` emits the TypeScript mirror of this module, so
edit here and regenerate rather than hand-editing the .ts file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Provider(StrEnum):
    OLLAMA = "ollama"
    OPENAI = "openai"


class QueryMode(StrEnum):
    """Retrieval strategy selected in chat and settings."""

    LOCAL = "local"
    GLOBAL = "global"
    DRIFT = "drift"
    AUTO = "auto"


class RagMode(StrEnum):
    REGULAR = "regular"
    AGENTIC = "agentic"


class Chunker(StrEnum):
    RECURSIVE = "recursive"
    SEMANTIC = "semantic"
    STRUCTURE_ENTITY = "structure_entity"
    CLAIM_CENTERED = "claim_centered"


class IngestEffort(StrEnum):
    MANUAL = "manual"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IngestMode(StrEnum):
    REGULAR = "regular"
    MULTIMODAL = "multimodal"


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


# ---------------------------------------------------------------------------
# Models and profiles
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    id: str
    provider: Provider
    label: str
    context_window: int = 8192
    supports_vision: bool = False
    supports_tools: bool = True
    is_embedding: bool = False
    dimensions: int | None = None
    available: bool = True
    unavailable_reason: str | None = None
    # Curated catalog / probe enrichments (optional; defaults keep older clients happy).
    parameter_b: float | None = None
    est_vram_mb: int | None = None
    size_bytes: int | None = None
    hf_id: str | None = None
    role_scores: dict[str, float] | None = None
    tier: str | None = None


class Profile(BaseModel):
    """One coherent configuration.

    Five independent toggles would allow 32 combinations, several of which
    cannot work. Bundling them here means :meth:`validate_profile` is the one
    place that knows which, and the UI derives its disabled states from it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = "default"
    name: str = "Default"

    chat_model: str = "qwen3.5:9b"
    chat_provider: Provider = Provider.OLLAMA

    # Fast/small model for the voice agent only; chat keeps chat_model.
    voice_model: str = "qwen3.5:2b"
    voice_provider: Provider = Provider.OLLAMA

    # Pinned once an index exists: changing it invalidates every stored vector.
    embedding_model: str = "qwen3-embedding:8b"
    embedding_provider: Provider = Provider.OLLAMA

    rag_mode: RagMode = RagMode.REGULAR
    query_mode: QueryMode = QueryMode.LOCAL
    ingest_mode: IngestMode = IngestMode.REGULAR

    # Manual picks chunker; low forces structure_entity; medium/high use the
    # decision model at ingest time (see docs/ingestion-plan.md).
    ingest_effort: IngestEffort = IngestEffort.MANUAL
    chunker: Chunker = Chunker.RECURSIVE
    chunk_size: int = Field(default=700, ge=128, le=4096)
    chunk_overlap: int = Field(default=100, ge=0, le=1024)

    # Fast/small chat model that picks or scores chunk strategies.
    chunk_decision_model: str = "qwen3.5:2b"
    chunk_decision_provider: Provider = Provider.OLLAMA

    # LLM used at index time for entity/relation extraction.
    extraction_model: str = "qwen3.5:2b"
    extraction_provider: Provider = Provider.OLLAMA

    # LLM that scores fused hits and grades agentic relevance.
    rerank_model: str = "qwen3.5:2b"
    rerank_provider: Provider = Provider.OLLAMA

    agentic_max_iters: int = Field(default=3, ge=1, le=8)
    rrf_k: int = Field(default=60, ge=1, le=200)
    hybrid_vector_top_k: int = Field(default=20, ge=1, le=100)
    hybrid_keyword_top_k: int = Field(default=20, ge=1, le=100)

    # Obsidian notes are terse; without the title and heading path the
    # extractor cannot resolve pronouns. Highest-leverage knob for a vault.
    prepend_note_context: bool = True

    # Expand a retrieved chunk to its enclosing heading section by reading the
    # note from disk, rather than maintaining a second parent-document index.
    expand_to_parent: bool = True

    community_level: int = Field(default=2, ge=0, le=4)
    max_context_tokens: int = Field(default=8000, ge=512)
    top_k: int = Field(default=10, ge=1, le=100)

    tracing_enabled: bool = False

    # Opt-in Hugging Face Hub enrichment for role recommendations (default off).
    model_metrics_online: bool = False


class ValidationIssue(BaseModel):
    level: IssueLevel
    field: str
    message: str


class ProfileValidation(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class OptionValidity(BaseModel):
    field: str
    value: str
    valid: bool
    reason: str = ""


class ProfileMatrix(BaseModel):
    """Which choices would break the current profile, and why.

    Computed server-side so the UI can grey out options without reimplementing
    any of the rules in :func:`validate_profile`.
    """

    current: ProfileValidation
    options: list[OptionValidity] = Field(default_factory=list)


def validate_profile(
    profile: Profile, models: dict[str, ModelInfo] | None = None
) -> ProfileValidation:
    """Reject impossible combinations and flag expensive ones.

    Errors block the run. Warnings are surfaced but permitted, because "slow"
    is the user's call to make and "impossible" is not.
    """
    models = models or {}
    issues: list[ValidationIssue] = []

    chat = models.get(profile.chat_model)

    if profile.ingest_mode is IngestMode.MULTIMODAL:
        # A ColPali index stores page-image patch vectors. There are no
        # entities and no communities to map-reduce over.
        if profile.query_mode in (QueryMode.GLOBAL, QueryMode.DRIFT):
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field="query_mode",
                message="Global and DRIFT need community summaries, which a visual "
                        "index does not build. Use Local, or switch to regular ingestion.",
            ))
        # Retrieval returns page images, so the answering model must see them.
        if chat is not None and not chat.supports_vision:
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field="chat_model",
                message=f"{chat.label} cannot read images. Visual retrieval returns page "
                        "images, so pick a vision model such as qwen2.5vl or gpt-4o.",
            ))
        if profile.chunker is Chunker.SEMANTIC:
            issues.append(ValidationIssue(
                level=IssueLevel.WARNING,
                field="chunker",
                message="Chunking is unused for visual ingestion; pages are embedded whole.",
            ))
        if profile.ingest_effort is not IngestEffort.MANUAL:
            issues.append(ValidationIssue(
                level=IssueLevel.WARNING,
                field="ingest_effort",
                message="Visual ingestion embeds pages whole; text chunk effort is ignored.",
            ))

    # Local / DRIFT / Auto need embeddings for hybrid entry points.
    if profile.query_mode in (QueryMode.LOCAL, QueryMode.DRIFT, QueryMode.AUTO):
        if not profile.embedding_model:
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field="embedding_model",
                message="This query mode needs an embedding model for hybrid retrieval.",
            ))

    if not profile.extraction_model:
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="extraction_model",
            message="Reindex needs an extraction model to build the entity graph.",
        ))

    if not profile.rerank_model:
        issues.append(ValidationIssue(
            level=IssueLevel.ERROR,
            field="rerank_model",
            message="A rerank model is required to score fused hits.",
        ))

    if profile.rag_mode is RagMode.AGENTIC and profile.query_mode is QueryMode.GLOBAL:
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="rag_mode",
            message="Each agentic retry may re-run global map-reduce. "
                    "Expect minutes per turn on a local model.",
        ))

    if profile.query_mode is QueryMode.GLOBAL and profile.community_level >= 3:
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="community_level",
            message="Deeper levels hold more, smaller reports: better answers, "
                    "proportionally more LLM calls.",
        ))

    if profile.chunk_overlap >= profile.chunk_size:
        issues.append(ValidationIssue(
            level=IssueLevel.ERROR,
            field="chunk_overlap",
            message="Overlap must be smaller than chunk size.",
        ))

    if profile.chunk_size > 1200:
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="chunk_size",
            message="Larger chunks cost less but extract markedly fewer entities. "
                    "600-800 tokens is the sweet spot for a personal vault.",
        ))

    if (
        profile.ingest_effort is IngestEffort.MANUAL
        and profile.chunker is Chunker.SEMANTIC
        and profile.ingest_mode is IngestMode.REGULAR
    ):
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="chunker",
            message="Semantic chunking costs an embedding call per sentence. "
                    "Structure + wikilink-aware is usually better for an Obsidian vault.",
        ))

    if (
        profile.ingest_effort is IngestEffort.LOW
        and profile.chunker is not Chunker.STRUCTURE_ENTITY
    ):
        issues.append(ValidationIssue(
            level=IssueLevel.WARNING,
            field="chunker",
            message="Low effort always uses structure + wikilink-aware chunking; "
                    "the manual chunker choice is ignored.",
        ))

    if profile.ingest_effort in (IngestEffort.MEDIUM, IngestEffort.HIGH):
        if not profile.chunk_decision_model:
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field="chunk_decision_model",
                message="Medium and high effort need a fast decision model to "
                        "choose or score chunking strategies.",
            ))

    for name, field_name in (
        (profile.chat_model, "chat_model"),
        (profile.voice_model, "voice_model"),
        (profile.embedding_model, "embedding_model"),
        (profile.chunk_decision_model, "chunk_decision_model"),
        (profile.extraction_model, "extraction_model"),
        (profile.rerank_model, "rerank_model"),
    ):
        if not name:
            continue
        if (
            field_name == "chunk_decision_model"
            and profile.ingest_effort not in (IngestEffort.MEDIUM, IngestEffort.HIGH)
            and profile.query_mode is not QueryMode.AUTO
        ):
            continue
        info = models.get(name)
        if info is not None and not info.available:
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field=field_name,
                message=info.unavailable_reason or f"{name} is not available.",
            ))
        elif field_name in (
            "voice_model",
            "chunk_decision_model",
            "extraction_model",
            "rerank_model",
        ) and models and name not in models:
            if field_name == "chunk_decision_model" and profile.query_mode is not QueryMode.AUTO:
                if profile.ingest_effort not in (IngestEffort.MEDIUM, IngestEffort.HIGH):
                    continue
            issues.append(ValidationIssue(
                level=IssueLevel.ERROR,
                field=field_name,
                message=f"{name} was not found. Pull it in Ollama or pick another model.",
            ))

    return ProfileValidation(
        valid=not any(i.level is IssueLevel.ERROR for i in issues),
        issues=issues,
    )


# Fields the settings screen renders as a fixed set of choices.
ENUMERABLE_FIELDS: dict[str, type[StrEnum]] = {
    "query_mode": QueryMode,
    "rag_mode": RagMode,
    "chunker": Chunker,
    "ingest_mode": IngestMode,
    "ingest_effort": IngestEffort,
}


def profile_matrix(
    profile: Profile, models: dict[str, ModelInfo] | None = None
) -> ProfileMatrix:
    """Validate the profile once per candidate value of each choice field."""
    models = models or {}
    options: list[OptionValidity] = []

    for field_name, enum in ENUMERABLE_FIELDS.items():
        for member in enum:
            candidate = profile.model_copy(update={field_name: member})
            result = validate_profile(candidate, models)
            blocking = [i for i in result.issues if i.level is IssueLevel.ERROR]
            # Chunker is chosen by effort (or later by the decision model); grey
            # out the picker without invalidating the saved profile.
            if (
                field_name == "chunker"
                and profile.ingest_effort is not IngestEffort.MANUAL
            ):
                blocking = [
                    ValidationIssue(
                        level=IssueLevel.ERROR,
                        field="chunker",
                        message="Switch to Manual effort to pick a chunker yourself.",
                    )
                ]
            options.append(
                OptionValidity(
                    field=field_name,
                    value=member.value,
                    valid=not blocking,
                    reason=blocking[0].message if blocking else "",
                )
            )

    for field_name, candidates in (
        ("chat_model", [m for m in models.values() if not m.is_embedding]),
        ("voice_model", [m for m in models.values() if not m.is_embedding]),
        ("embedding_model", [m for m in models.values() if m.is_embedding]),
        ("chunk_decision_model", [m for m in models.values() if not m.is_embedding]),
        ("extraction_model", [m for m in models.values() if not m.is_embedding]),
        ("rerank_model", [m for m in models.values() if not m.is_embedding]),
    ):
        for info in candidates:
            update: dict[str, Any] = {field_name: info.id}
            provider_field = {
                "voice_model": "voice_provider",
                "chunk_decision_model": "chunk_decision_provider",
                "extraction_model": "extraction_provider",
                "rerank_model": "rerank_provider",
            }.get(field_name)
            if provider_field:
                update[provider_field] = info.provider
            candidate = profile.model_copy(update=update)
            result = validate_profile(candidate, models)
            blocking = [i for i in result.issues if i.level is IssueLevel.ERROR]
            options.append(
                OptionValidity(
                    field=field_name,
                    value=info.id,
                    valid=not blocking,
                    reason=blocking[0].message if blocking else "",
                )
            )

    return ProfileMatrix(current=validate_profile(profile, models), options=options)


# ---------------------------------------------------------------------------
# Retrieval results
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """A pointer back into the vault.

    Offsets are into the note as it exists on disk, so the UI can highlight the
    exact span and the backend can expand to the enclosing section without
    keeping a copy of the text anywhere.
    """

    id: str
    note_path: str
    note_title: str
    heading_path: list[str] = Field(default_factory=list)
    snippet: str = ""
    char_start: int = 0
    char_end: int = 0
    score: float = 0.0
    source: Literal["graph", "vector", "visual", "link"] = "vector"
    page: int | None = None


class IndexStatus(BaseModel):
    engine: str = "mock"
    ready: bool = False
    indexing: bool = False
    # True when DB says indexing but this process has no live reindex task.
    indexing_stale: bool = False
    total_notes: int = 0
    indexed_notes: int = 0
    stale_notes: int = 0
    entities: int = 0
    relationships: int = 0
    communities: int = 0
    # Recorded at index time. Mixing extraction models produces duplicate
    # entities that never merge, so this is shown read-only in settings.
    embedding_model: str | None = None
    extraction_model: str | None = None
    last_indexed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str


class RetrievalStartEvent(BaseModel):
    type: Literal["retrieval_start"] = "retrieval_start"
    mode: QueryMode
    label: str
    # Broad search fans out one call per community report. Telling the UI up
    # front is what stops a legitimate multi-minute run from looking like a hang.
    estimated_calls: int = 1
    estimated_seconds: float | None = None


class RetrievalProgressEvent(BaseModel):
    type: Literal["retrieval_progress"] = "retrieval_progress"
    current: int
    total: int
    label: str = ""


class CitationsEvent(BaseModel):
    type: Literal["citations"] = "citations"
    citations: list[Citation]


class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str
    ok: bool = True
    result: str = ""


class ApprovalRequiredEvent(BaseModel):
    """A tool call the policy permits only with a human in the loop."""

    type: Literal["approval_required"] = "approval_required"
    id: str
    tool: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str
    code: str = "error"
    recoverable: bool = True


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    message_id: str
    cancelled: bool = False
    elapsed_ms: int = 0


StreamEvent = Annotated[
    TokenEvent
    | RetrievalStartEvent
    | RetrievalProgressEvent
    | CitationsEvent
    | ToolCallEvent
    | ToolResultEvent
    | ApprovalRequiredEvent
    | ErrorEvent
    | DoneEvent,
    Field(discriminator="type"),
]

StreamEventAdapter: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


# ---------------------------------------------------------------------------
# Requests and API responses
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """One prior chat turn. Client ``system`` roles are rejected at the API."""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Incoming chat request payload (SSE stream on POST /api/chat)."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="The user's message to the AI assistant",
    )
    history: list[ChatMessage] = Field(default_factory=list)
    profile: Profile = Field(default_factory=Profile)
    # Prefer thread_id; conversation_id is kept so the existing UI keeps working.
    thread_id: str = Field(
        default="default",
        description="Conversation thread ID",
    )
    conversation_id: str | None = Field(
        default=None,
        description="Alias for thread_id used by earlier clients",
    )
    approval_id: str | None = None

    def resolved_thread_id(self) -> str:
        """Single ID for logging and future thread persistence."""
        if self.conversation_id:
            return self.conversation_id
        return self.thread_id


class ChatResponse(BaseModel):
    """Non-streaming chat payload.

    The live UI uses SSE (:data:`StreamEvent`). This model is for a future
    unary endpoint or for callers that buffer the stream themselves.
    """

    response: str
    thread_id: str
    model_used: str
    cached: bool = False
    processing_time_ms: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())


class HealthResponse(BaseModel):
    """Health check response.

    ``checks`` includes ``obsidian_plugin``, ``vault_configured``,
    ``postgres_configured``, and ``postgres_ready`` (false when unset).
    Overall ``ok``/``status`` reflect core readiness (vault; Postgres when
    required) — Obsidian is reported but does not gate health.
    """

    status: str = "healthy"
    environment: str
    version: str = "0.1.0"
    # Kept for the existing frontend tooltip; mirrors status == "healthy".
    ok: bool = True
    checks: dict[str, Any] = Field(default_factory=dict)


class MetricsResponse(BaseModel):
    """Metrics endpoint response."""

    total_requests: int
    total_errors: int
    error_rate: str
    avg_latency_ms: float
    cache_hit_rate: str
    total_input_tokens: int
    total_output_tokens: int
    config_changes: int = 0
    # In-process rate-limit denials (all scopes) since process start.
    rate_limit_denials: int = 0
    # Requests that skipped rate limiting (loopback / health / path exempt).
    rate_limit_exemptions: int = 0


class ErrorResponse(BaseModel):
    """Standard error body for failed HTTP requests (not SSE ErrorEvent)."""

    error: str
    detail: str | None = None
    request_id: str | None = None


class ApprovalDecision(BaseModel):
    request_id: str
    tool: str
    approved: bool


class OptionsResponse(BaseModel):
    chat_models: list[ModelInfo]
    embedding_models: list[ModelInfo]
    query_modes: list[dict[str, str]]
    rag_modes: list[dict[str, str]]
    chunkers: list[dict[str, str]]
    ingest_modes: list[dict[str, str]]
    ingest_efforts: list[dict[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# System probe + model recommendations
# ---------------------------------------------------------------------------

RECOMMEND_ROLES = (
    "chat",
    "voice",
    "embedding",
    "chunk_decision",
    "extraction",
    "rerank",
)


class GpuInfo(BaseModel):
    """One GPU reported by a best-effort hardware probe."""

    name: str
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None


class SystemInfo(BaseModel):
    """Local machine specs used to gate and score model recommendations."""

    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    cpu_cores: int | None = None
    gpus: list[GpuInfo] = Field(default_factory=list)
    probe_errors: list[str] = Field(default_factory=list)


class RecommendRequest(BaseModel):
    """Rank catalog models for one or more profile roles."""

    roles: list[str] | None = None
    # Reserved for future server-side apply; today the UI always patches draft.
    apply: bool = False
    top_n: int = Field(default=5, ge=1, le=20)
    # When set, overrides Profile.model_metrics_online for this request only.
    online: bool | None = None
    # Optional draft profile (Settings may not have saved yet).
    profile: Profile | None = None


class ModelRecommendation(BaseModel):
    """One ranked candidate for a role."""

    id: str
    provider: Provider
    score: float
    reasons: list[str] = Field(default_factory=list)
    fits: bool = True
    needs_pull: bool = False
    metrics_degraded: bool = False
    available: bool = True
    disabled_reason: str = ""


class RoleRecommendation(BaseModel):
    """Ranked shortlist for a single profile role."""

    role: str
    recommendations: list[ModelRecommendation] = Field(default_factory=list)
    metrics_degraded: bool = False


class RecommendResponse(BaseModel):
    """Per-role ranked recommendations plus the system snapshot used to score."""

    roles: list[RoleRecommendation] = Field(default_factory=list)
    system: SystemInfo
    online: bool = False
    metrics_degraded: bool = False


# ---------------------------------------------------------------------------
# Local TTS (Fish Speech / OpenAudio S1-mini)
# ---------------------------------------------------------------------------


class TtsRequest(BaseModel):
    """Text to synthesize with the local Fish Speech server."""

    text: str = Field(..., min_length=1, max_length=8000)


class TtsStatus(BaseModel):
    """Whether Fish Speech TTS is enabled and the local server is up."""

    enabled: bool = True
    ready: bool = False
    engine: str = "fish-speech"
    model: str = "openaudio-s1-mini"
    base_url: str = "http://127.0.0.1:8080"


# ---------------------------------------------------------------------------
# Engine protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RetrievalEngine(Protocol):
    """What the rest of the app requires of a retrieval backend."""

    name: str

    def query(
        self, question: str, profile: Profile, history: list[ChatMessage] | None = ...
    ) -> AsyncIterator[StreamEvent]:
        """Stream the answer. Must yield DoneEvent last, including on cancel."""
        ...

    async def index_status(self) -> IndexStatus:
        """Return current index readiness and vault/index stats for the UI."""
        ...
