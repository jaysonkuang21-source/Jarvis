# Retrieval architecture

Postgres-backed hybrid retrieval for Jarvis. Complements
[`ingestion-plan.md`](ingestion-plan.md).

## Locked decisions

- **Local leaf order**: metadata filter (open-normalized document tags via
  `documents.tags @>`, filled from the question by `rerank_model` when the
  caller omits filters; empty-tag miss falls back to unfiltered) → result cache
  (`JARVIS_CACHE_TTL` / `cache_ttl_seconds`) → hybrid (pgvector ANN/HNSW +
  Postgres FTS + **RRF**, default `k=60`) → entity neighborhood (1–2 hops) →
  LLM rerank → answer LLM.
- **Reranker**: LLM relevance scoring of top fused hits (`rerank_model`).
- **Query modes**: Local, Global, DRIFT, Auto.
- **Rag modes**: Regular vs Agentic (grade → rewrite → retry).
- Engine id: `postgres-hybrid`. Fallback: `placeholder` when
  `JARVIS_DATABASE_URL` is unset or unreachable.

## Query modes

| Mode | Behavior |
|------|----------|
| Local | Filter → cache → hybrid → entity neighborhood → rerank |
| Global | Map-reduce over community reports |
| DRIFT | Probe communities → constrained Local into top regions → merge |
| Auto | Router LLM picks a mode (rewrite deferred) |

## Orchestration

Chat retrieval + answer generation for `postgres-hybrid` is orchestrated by
**LangGraph** (`app/retrieval/graph.py`): resolve mode → retrieve (hybrid leaf
for Local/DRIFT) → optional agentic grade/rewrite → expand → citations →
generate (primary + one retry) → graceful error. Custom stream mode feeds SSE
events (`RetrievalStart` / progress / citations / tokens).

## Agentic loop

1. Retrieve with the active (or Auto-resolved) mode.
2. Grade docs with the rerank/grade LLM.
3. If relevant → expand / answer.
4. If empty → graceful failure (no docs).
5. Else rewrite query and retry until `agentic_max_iters`, then answer on last chunks.

## Module map

| Path | Role |
|------|------|
| `app/db/` | Pool, DDL, repository helpers |
| `app/ingestion/index.py` | Vault walk, prepare, embed, extract, communities |
| `app/ingestion/embeddings.py` | Embed helpers (index + query) |
| `app/retrieval/hybrid.py` | Metadata filter + vector + FTS + RRF |
| `app/retrieval/result_cache.py` | TTL cache for ranked chunk lists |
| `app/retrieval/rerank.py` | LLM rerank / grade / rewrite |
| `app/retrieval/modes.py` | Local, Global, DRIFT, Auto |
| `app/retrieval/graph.py` | LangGraph StateGraph + SSE stream bridge |
| `app/retrieval/agentic.py` | Back-compat grade/rewrite helper + route re-exports |
| `app/retrieval/expand.py` | Parent-section expand + citations |
| `app/retrieval/engine.py` | `RetrievalEngine` implementation |

## Config

- `JARVIS_DATABASE_URL` — e.g. `postgresql://jarvis:jarvis@127.0.0.1:5432/jarvis`
- Requires Postgres with the `vector` extension (`pgvector`). Local lab: root
  `docker-compose.yml` + `scripts/db-up.ps1` / `scripts/db-down.ps1` (see README).
- `JARVIS_CACHE_TTL` / `cache_ttl_seconds` — retrieval result cache TTL (default 300).
