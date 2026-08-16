# Ingestion architecture

Jarvis indexes an Obsidian vault into **Postgres** for hybrid retrieval.
See also [`retrieval-plan.md`](retrieval-plan.md).

## Locked decisions

- **Vault markdown** is the source of truth for note text and citation offsets.
- **Prep always runs first**: clean/normalize body text; OCR embedded images
  (`![[img]]` / `![]()` ) via system `tesseract` when available. Chunk offsets
  are mapped back to on-disk spans for citations.
- **Document tags**: frontmatter tags plus open freeform tags suggested by
  `rerank_model` at reindex (`app/ingestion/tags.py`), merged and slug-
  normalized onto `documents.tags` for pre-vector filtering. All chunkers except
  `recursive` call the model; reindex refreshes tags even when note content is
  unchanged (hash skip no longer skips tag imbuement).
- **Uploads**: any file type via Settings → Ingestion → Add documents. Originals
  land in `Inbox/files/`; a companion `.md` note is tagged with `kind-*` and
  `retriever-*` (`text-hybrid` / `visual` / `binary-meta`). PDFs are text-
  extracted with **pypdf**; **`.docx`** uses OOXML paragraph extraction (stdlib).
  Query cues like “screenshot” / “pdf” select the matching filter before hybrid
  search.
- **Removal**: `DELETE /api/index/documents` drops a row from Postgres (optional
  vault trash for the note + `source_file`). Reindex also prunes index rows whose
  `.md` notes were deleted from the vault.
- **Postgres** holds evidence vectors (pgvector), keyword/FTS, entities,
  relationships, and community reports. No Neo4j.
- **Graph edges** come from Obsidian `[[wikilinks]]` plus LLM extraction at
  index time (`extraction_model` on the profile).
- **Chunking** stays in `app/ingestion/` (effort modes + LangChain/Chonkie
  splitters). Extraction is a post-chunk index stage, not a text splitter.
- **Embeddings** live in `app/ingestion/embeddings.py` (shared by index + query).
- **Retrieval** lives in `app/retrieval/`. `app/agent.py` keeps model registry
  and chat constructors only.

```text
DOCUMENT (vault note)
    │
    ▼
Prepare (normalize + OCR)
    │
    ▼
Chunk (effort / chunker)
    │
    ├──────────────────┐
    ▼                  ▼
Embed → pgvector    LLM extract + wikilinks → entities/relationships
    │                  │
    └────────┬─────────┘
             ▼
        Communities + reports
             │
             ▼
        Hybrid retrieval (see retrieval-plan.md)
```

## Ingestion effort modes

| Effort | Behavior |
|--------|----------|
| Manual | User picks a chunker. |
| Low | Structure + wikilink-aware (`structure_entity`). |
| Medium | Fast LLM picks among structure, claim, semantic, recursive. |
| High | Score multiple chunkers (connectivity, context loss, metadata). |

Profile fields: `ingest_effort`, `chunker`, `chunk_decision_model`,
`chunk_size`, `chunk_overlap`, `prepend_note_context`, plus
`extraction_model` / `extraction_provider` for the extract stage.

### Chunkers

| Value | Role |
|-------|------|
| `recursive` | Markdown recursive + tiktoken budget. |
| `semantic` | Embedding breakpoints when embeddings are supplied. |
| `structure_entity` | Headers + never cut `[[wikilinks]]`. |
| `claim_centered` | Sentence-oriented (Chonkie). |

## Evidence row metadata

Pydantic `EvidenceChunk` fields (richer on structure / semantic / claim):

- `document_id` — vault-relative path
- `doc_title`, `heading_path`, `section` (leaf heading), `page` (optional; markdown → null)
- `tags` — from note frontmatter; also stored on `documents.tags` for retrieval filters
- `chunk_id` — stable id within the note
- `entity_ids` / relationship links via join tables
- `char_start`, `char_end` — offsets into the **on-disk** note
