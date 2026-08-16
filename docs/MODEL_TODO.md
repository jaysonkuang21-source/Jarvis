# Models TODO

Checklist of models and ingest decisions Jarvis still needs. Mirrored in the
in-app **Models TODO** view (`frontend/src/lib/modelTodo.ts`).

## Open / partial

### Embedding model — PARTIAL

- **Hook:** `app/ingestion/embeddings.py`, `profile.embedding_model` /
  `embedding_provider`, dim guard in `app/ingestion/dim_guard.py`
- **Status:** Catalog + profile wiring exist (Ollama / OpenAI). Still need a
  hardened production default, multimodal (ColPali) path, and clear migration
  when dimensions change.
- **UI:** Settings → Models → Embedding

### Speaking / TTS + push-to-talk — DONE

- **Hook:** `frontend/src/lib/speech/` · `app/tts` (Fish Speech HTTP) · radar
  hold-to-talk · `chat.ts` / `voice.ts` speak on done · `app/voice` uses
  `profile.voice_model` (default `qwen3.5:2b`, independent of `chat_model`)
- **Status:** Local **OpenAudio S1-mini** via Fish Speech `POST /v1/tts` (server
  separate from Jarvis — `scripts/fish-speech-up.ps1`). Voice path streams tokens
  with Ollama `reasoning=False` (no `<think>`), speaks sentence-by-sentence while
  the LLM streams. Chat spoken replies wait until the LLM `done` event so Fish
  and chat Qwen do not share the GPU mid-stream. Falls back to Web Speech only
  when Fish is down. Fixed seed + lower temperature keep the default voice
  stabler; pin `JARVIS_TTS_REFERENCE_ID` for a locked timbre.
- **UI:** Radar core hold + chat mute; hub Wake says “Ready.” Settings → Models
  → Voice model (separate from Chat).
- **Config:** `JARVIS_TTS_*`; `JARVIS_OLLAMA_KEEP_ALIVE` /
  `JARVIS_OLLAMA_VOICE_KEEP_ALIVE` / optional `JARVIS_OLLAMA_WARM_ON_BOOT`;
  profile `voice_model` / `voice_provider` in `config/profiles.json` (not an env
  override).
- **Note:** Public demo users should keep Web Speech (or later cloud/BYOK);
  S1-mini is for self-hosted personal use (~5GB VRAM). Jarvis autostarts the
  Fish Docker container on boot when weights exist (`JARVIS_TTS_AUTOSTART`).
  Weights are gated on Hugging Face — accept terms before
  `scripts/fish-speech-up.ps1` can download them. Keep voice on a small model so
  Fish + Ollama can coexist; chat TTS is sequenced after the LLM. Check
  `ollama ps` when debugging VRAM thrash.

### Evaluator model (agentic RAG) — PARTIAL

- **Hook:** `app/retrieval/rerank.py` → `grade_relevant`; LangGraph node in
  `app/retrieval/graph.py` (agentic branch); uses `profile.rerank_model` today
- **Status:** Grading works via the shared rerank model. Missing a dedicated
  evaluator model field and tuned prompts.
- **UI:** Settings → Retrieval → Rerank model (temporary stand-in)

### Query rewrite model — PARTIAL

- **Hook:** `app/retrieval/rerank.py` → `rewrite_query` inside the LangGraph
  agentic rewrite node
- **Status:** Rewrites reuse `rerank_model`. Prefer a separate lightweight rewrite
  model or explicit profile field.
- **UI:** same as evaluator until split

### Writing metadata in chunking — PARTIAL

- **Hook:** `app/ingestion/tags.py`, `app/ingestion/index.py`,
  `app/retrieval/modes.py`
- **Status:** Document tags are open-normalized (frontmatter + `rerank_model`
  imbuement at reindex). Local/DRIFT extract query tags before hybrid ANN and
  fall back to unfiltered search on miss. Heading paths / tags / document ids
  attach. Richer claim, entity, and visual metadata still to land.
- **UI:** Settings → Models → Rerank / evaluator model (also used for tagging)

### Choosing the best chunker — PARTIAL

- **Hook:** `app/ingestion/effort.py` → `resolve_chunk_plan`;
  `profile.chunk_decision_model` / ingest effort medium & high
- **Status:** Medium/high effort still fall back to `structure_entity` with
  `needs_decision_model=True`. Decision LLM + multi-chunker scoring not shipped.
- **UI:** Settings → Ingestion → Effort / Chunker

## Done enough for day-to-day

- Chat model selection (Ollama + OpenAI) via profile
- Voice model selection (default tiny `qwen3.5:2b`; chat stays on `chat_model`)
- Regular vs agentic RAG mode toggle
- Manual / low ingest effort chunker plans
- Browser push-to-talk STT + spoken replies (Fish Speech S1-mini + Web Speech fallback)
- **Model role recommender (DONE):** Settings catalog + per-role Recommend for
  chat / voice / embedding / chunk_decision / extraction / rerank; hardware probe
  (`GET /api/system`); curated `config/model_catalog.json`; opt-in HF Hub
  metrics (`JARVIS_MODEL_METRICS_ONLINE`, default off).

## How to keep this in sync

Update both this file and `MODEL_TODO_ITEMS` in
`frontend/src/lib/modelTodo.ts` when a model path lands.
