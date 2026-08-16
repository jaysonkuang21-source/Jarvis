/**
 * Shared model/implementation backlog shown in-app and mirrored in docs/MODEL_TODO.md.
 */

export type ModelTodoStatus = 'open' | 'partial' | 'done'

export interface ModelTodoItem {
  id: string
  title: string
  status: ModelTodoStatus
  /** Where this plugs into the backend today. */
  hook: string
  note: string
}

/** Checklist of models / ingest choices still incomplete for Jarvis. */
export const MODEL_TODO_ITEMS: ModelTodoItem[] = [
  {
    id: 'recommender',
    title: 'Model role recommender',
    status: 'done',
    hook: 'app/models/recommend.py · GET /api/system · POST /api/models/recommend',
    note: 'Per-role Recommend/Apply in Settings; curated catalog + opt-in HF metrics (default off).',
  },
  {
    id: 'embedding',
    title: 'Embedding model',
    status: 'partial',
    hook: 'app/ingestion/embeddings.py · profile.embedding_model',
    note: 'Profile + Ollama/OpenAI catalogs exist; harden dim-guard, production defaults, and ColPali multimodal path.',
  },
  {
    id: 'tts',
    title: 'Speaking / TTS + push-to-talk',
    status: 'done',
    hook: 'frontend/src/lib/speech/* · app/tts (Fish S1-mini HTTP) · RadarHub · chat/voice stream speak',
    note: 'Fish S1-mini streaming PCM into AudioWorklet ring; short opening then long chunks + depth-3 prefetch; Ollama keep_alive.',
  },
  {
    id: 'evaluator',
    title: 'Evaluator model (agentic RAG)',
    status: 'partial',
    hook: 'app/retrieval/rerank.py · grade_relevant · graph.py · profile.rerank_model',
    note: 'Uses rerank model as grader today (LangGraph agentic branch). Dedicated evaluator field still TODO.',
  },
  {
    id: 'query-rewrite',
    title: 'Query rewrite model',
    status: 'partial',
    hook: 'app/retrieval/rerank.py · rewrite_query · LangGraph rewrite node',
    note: 'Shares rerank_model. Consider a separate rewrite model or lighter draft model for retries.',
  },
  {
    id: 'chunk-metadata',
    title: 'Writing metadata in chunking',
    status: 'partial',
    hook: 'app/ingestion/tags.py · index.py · modes.py tag prefilter',
    note: 'Open-normalized doc tags (frontmatter + rerank_model) and query-tag prefilter with unfiltered fallback. Claim/entity/visual metadata still TODO.',
  },
  {
    id: 'chunker-select',
    title: 'Choosing the best chunker',
    status: 'partial',
    hook: 'app/ingestion/effort.py · chunk_decision_model',
    note: 'Medium/high effort still fall back to structure_entity; decision + multi-score path not shipped.',
  },
]

/** Map status codes to display labels for the ops UI. */
export function modelTodoStatusLabel(status: ModelTodoStatus): string {
  switch (status) {
    case 'open':
      return 'OPEN'
    case 'partial':
      return 'PARTIAL'
    case 'done':
      return 'DONE'
  }
}
