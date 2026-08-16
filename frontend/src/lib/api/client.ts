/** Typed client for the Jarvis backend. */

import { describeApiFailure, type ApiErrorCode } from './errors'
import { readSse } from './sse'
import type {
  ApprovalDecision,
  ChatMessage,
  CreateJobRequest,
  HealthResponse,
  IndexStatus,
  Job,
  MetricsResponse,
  OptionsResponse,
  Policy,
  Profile,
  ProfileMatrix,
  ProfileValidation,
  RecommendRequest,
  RecommendResponse,
  StreamEvent,
  SystemInfo,
  TtsStatus,
} from './types'
import { isDemoMode } from '@/lib/demo'
import { sessionLlmHeaders } from '@/lib/sessionLlm'

// Vite proxies /api in local dev. Demo/production builds may point at Render
// via VITE_API_BASE_URL (scheme+host, no trailing slash).
const API_ORIGIN = (
  typeof import.meta.env.VITE_API_BASE_URL === 'string'
    ? import.meta.env.VITE_API_BASE_URL.trim().replace(/\/$/, '')
    : ''
)
const BASE = API_ORIGIN ? `${API_ORIGIN}/api` : '/api'

/**
 * Session API token for Bearer / X-Jarvis-Token (module memory only).
 * Never log this value, never write it to localStorage/sessionStorage, and
 * never put it in URL query strings. Prefer the Tauri-minted token in
 * desktop builds; demo builds use the Supabase access token instead.
 */
let apiToken: string | null = null

/** Store the shared secret in memory for protected API calls (not persisted). */
export function setApiToken(token: string | null): void {
  const trimmed = token?.trim() ?? ''
  apiToken = trimmed.length > 0 ? trimmed : null
}

/**
 * Resolve the API token for protected routes.
 *
 * Demo builds prefer the live Supabase access token. Desktop prefers Tauri
 * ``get_api_token``. Browser Vite falls back to ``VITE_JARVIS_API_TOKEN``.
 */
export async function bootstrapApiToken(): Promise<void> {
  if (isDemoMode) {
    try {
      const { getAccessToken } = await import('@/lib/supabase')
      setApiToken(await getAccessToken())
    } catch {
      setApiToken(null)
    }
    return
  }

  try {
    const { invoke } = await import('@tauri-apps/api/core')
    const token = await invoke<string>('get_api_token')
    if (typeof token === 'string' && token.trim()) {
      setApiToken(token.trim())
    }
    return
  } catch {
    // Tauri unavailable (plain browser / Vite): fall through to env.
  }

  const fromEnv = import.meta.env.VITE_JARVIS_API_TOKEN
  if (typeof fromEnv === 'string' && fromEnv.trim()) {
    setApiToken(fromEnv.trim())
  }
}

/** Headers that carry the API token when one is configured (never logged). */
function authHeaders(): Record<string, string> {
  if (!apiToken) return {}
  // Demo uses Supabase JWT — only Bearer. Desktop also sends X-Jarvis-Token.
  if (isDemoMode) {
    return { Authorization: `Bearer ${apiToken}` }
  }
  return {
    Authorization: `Bearer ${apiToken}`,
    'X-Jarvis-Token': apiToken,
  }
}

export class ApiError extends Error {
  status: number
  code: ApiErrorCode

  /** Create an API error with HTTP status and a stable classification code. */
  constructor(message: string, status: number, code: ApiErrorCode = 'unknown') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/** Issue a JSON request against the backend and throw ApiError on failure. */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...authHeaders(),
  }
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
    })
  } catch {
    throw new ApiError(
      'Cannot reach the Jarvis backend. Is it running?',
      0,
      'backend_unreachable',
    )
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    const { code, message } = describeApiFailure(
      response.status,
      detail || '',
      response.statusText,
    )
    throw new ApiError(message, response.status, code)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface NoteResponse {
  path: string
  title: string
  content: string
  section_start: number
  section_end: number
}

export interface OpenNoteResponse {
  opened: boolean
  uri: string
}

/** Result of writing one or more notes into the vault Inbox. */
export interface DeleteDocumentResponse {
  removed_from_index: boolean
  vault_files_trashed: string[]
}

export interface IngestNotesResponse {
  paths: string[]
  count: number
  documents?: Array<{
    note_path: string
    file_path?: string | null
    kind: string
    retriever: string
    tags: string[]
  }>
}

/** One indexed chunk returned by the document chunk inspector. */
export interface DocumentChunkPreview {
  chunk_id: string
  text: string
  heading_path: string[]
  char_start: number
  char_end: number
  note_path: string
  note_title: string
  tags?: string[]
  wikilinks?: string[]
  entities?: string[]
}

/** Chunk inventory for one vault path after reindex. */
export interface DocumentChunksResponse {
  path: string
  total: number
  chunks: DocumentChunkPreview[]
}

/** One indexed document for the cross-session chunk browser. */
export interface IndexedDocumentSummary {
  path: string
  title: string
  tags: string[]
  chunk_count: number
}

/** Inventory of documents currently in the Postgres index. */
export interface IndexedDocumentsResponse {
  documents: IndexedDocumentSummary[]
  total: number
}

/** Encode a browser File as base64 for the ingest upload API. */
async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

export const api = {
  /** Check backend health. */
  health: () => request<HealthResponse>('/health'),

  /** Fetch runtime metrics. */
  metrics: () => request<MetricsResponse>('/metrics'),

  /** Load model and mode options, optionally forcing a refresh. */
  options: (refresh = false) =>
    request<OptionsResponse>(`/options${refresh ? '?refresh=true' : ''}`),

  /** Load the saved profile. */
  getProfile: () => request<Profile>('/profile'),

  /** Persist a profile and return the saved copy. */
  saveProfile: (profile: Profile) =>
    request<Profile>('/profile', { method: 'PUT', body: JSON.stringify(profile) }),

  /** Validate a profile without saving it. */
  validateProfile: (profile: Profile, signal?: AbortSignal) =>
    request<ProfileValidation>('/profile/validate', {
      method: 'POST',
      body: JSON.stringify(profile),
      signal,
    }),

  /** Fetch the compatibility matrix for profile field choices. */
  profileMatrix: (profile: Profile, signal?: AbortSignal) =>
    request<ProfileMatrix>('/profile/matrix', {
      method: 'POST',
      body: JSON.stringify(profile),
      signal,
    }),

  /** Load the standing policy rules. */
  getRules: () => request<Policy>('/rules'),

  /** Persist policy rules and return the reloaded policy. */
  saveRules: (policy: Policy, confirmElevation = false) =>
    request<Policy>('/rules', {
      method: 'PUT',
      body: JSON.stringify({
        policy,
        confirm_elevation: confirmElevation,
      }),
    }),

  /** Fetch vault index readiness and stats. */
  indexStatus: () => request<IndexStatus>('/index/status'),

  /** Read a note from disk, optionally seeking near a character offset. */
  readNote: (path: string, charStart = 0) =>
    request<NoteResponse>(
      `/notes?path=${encodeURIComponent(path)}&char_start=${charStart}`,
    ),

  /** Ask the backend to open a note in Obsidian. */
  openNote: (path: string) =>
    request<OpenNoteResponse>('/notes/open', {
      method: 'POST',
      body: JSON.stringify({ path }),
    }),

  /** Paste a note into the vault Inbox for digestion. */
  ingestNotePaste: (body: { content: string; title?: string; filename?: string }) =>
    request<IngestNotesResponse>('/notes/ingest', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** Upload any files into the vault Inbox (base64 JSON; type picks retriever). */
  ingestNoteFiles: async (files: File[]): Promise<IngestNotesResponse> => {
    const payload = {
      files: await Promise.all(
        files.map(async (file) => ({
          filename: file.name,
          content_base64: await fileToBase64(file),
          mime: file.type || undefined,
        })),
      ),
    }
    return request<IngestNotesResponse>('/notes/ingest/upload', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  /** Start a background vault reindex into Postgres. Pass force to clear a stuck job. */
  reindex: (force = false) =>
    request<IndexStatus>(`/index/reindex${force ? '?force=true' : ''}`, {
      method: 'POST',
      body: '{}',
    }),

  /** Remove a document from the Postgres index; optionally trash vault files. */
  deleteIndexedDocument: (path: string, deleteVaultFiles = false) =>
    request<DeleteDocumentResponse>('/index/documents', {
      method: 'DELETE',
      body: JSON.stringify({ path, delete_vault_files: deleteVaultFiles }),
    }),

  /** List indexed chunks for a vault-relative path (chunk quality inspector). */
  listDocumentChunks: (path: string, limit = 500) =>
    request<DocumentChunksResponse>(
      `/index/documents/chunks?path=${encodeURIComponent(path)}&limit=${limit}`,
    ),

  /** List every indexed document (chunk browser across sessions). */
  listIndexedDocuments: () =>
    request<IndexedDocumentsResponse>('/index/documents'),

  /** Submit an allow/deny decision for a pending tool approval. */
  approve: (decision: ApprovalDecision) =>
    request<{ ok: boolean }>('/approvals', {
      method: 'POST',
      body: JSON.stringify(decision),
    }),

  /** List pending timer jobs. */
  timers: () => request<Job[]>('/timers'),

  /** Create a timer or scheduled job. */
  createTimer: (job: CreateJobRequest) =>
    request<Job>('/timers', { method: 'POST', body: JSON.stringify(job) }),

  /** Cancel a pending timer by id. */
  cancelTimer: (id: string) =>
    request<{ ok: boolean }>(`/timers/${id}`, { method: 'DELETE' }),

  /** Probe local RAM / CPU / GPU for Settings and recommendations. */
  system: () => request<SystemInfo>('/system'),

  /** Rank models for one or more profile roles (does not write the profile). */
  recommendModels: (body: Partial<RecommendRequest> & { roles?: string[] | null }) =>
    request<RecommendResponse>('/models/recommend', {
      method: 'POST',
      body: JSON.stringify({
        apply: false,
        top_n: 5,
        online: null,
        profile: null,
        roles: null,
        ...body,
      }),
    }),

  /** Status of local Fish Speech TTS (enabled + server reachable). */
  ttsStatus: () => request<TtsStatus>('/tts'),
}

/** Synthesize speech via local Fish Speech; returns a streaming PCM response. */
export async function synthesizeSpeechStream(
  text: string,
  signal?: AbortSignal,
): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${BASE}/tts`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ text }),
      signal,
    })
  } catch {
    throw new ApiError(
      'Cannot reach the Jarvis backend. Is it running?',
      0,
      'backend_unreachable',
    )
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    const { code, message } = describeApiFailure(
      response.status,
      detail || '',
      response.statusText,
    )
    throw new ApiError(message, response.status, code)
  }
  return response
}

/** Synthesize speech and buffer the full PCM body (tests / non-streaming callers). */
export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<Blob> {
  const response = await synthesizeSpeechStream(text, signal)
  return response.blob()
}

export interface ChatStreamOptions {
  message: string
  history: ChatMessage[]
  profile: Profile
  conversationId?: string
  approvalId?: string
  signal?: AbortSignal
  onEvent: (event: StreamEvent) => void
}

/**
 * Stream one chat turn.
 *
 * Aborting resolves rather than throwing: cancellation is a normal outcome
 * here, and callers should not have to special-case it.
 */
export async function streamChat(options: ChatStreamOptions): Promise<void> {
  const { signal, onEvent } = options

  let response: Response
  try {
    response = await fetch(`${BASE}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...authHeaders(),
        ...(isDemoMode ? sessionLlmHeaders() : {}),
      },
      body: JSON.stringify({
        message: options.message,
        history: options.history,
        profile: options.profile,
        thread_id: options.conversationId ?? 'default',
        conversation_id: options.conversationId ?? null,
        approval_id: options.approvalId ?? null,
      }),
      signal,
    })
  } catch (error) {
    if (isAbort(error)) return
    onEvent({
      type: 'error',
      message: `Cannot reach the Jarvis backend. Is it running on port 8756? (${String(error)})`,
      code: 'backend_unreachable',
      recoverable: true,
    })
    return
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    const { code, message } = describeApiFailure(
      response.status,
      detail || '',
      response.statusText,
    )
    onEvent({
      type: 'error',
      message,
      code: code === 'http_error' ? `http_${response.status}` : code,
      recoverable: response.status === 429 || response.status < 500,
    })
    return
  }

  try {
    for await (const frame of readSse(response, signal)) {
      let parsed: StreamEvent
      try {
        parsed = JSON.parse(frame.data) as StreamEvent
      } catch {
        continue
      }
      onEvent(parsed)
    }
  } catch (error) {
    if (isAbort(error)) return
    onEvent({
      type: 'error',
      message: `Stream interrupted: ${String(error)}`,
      code: 'stream_error',
      recoverable: true,
    })
  }
}

/**
 * Stream one voice-agent turn (direct LLM; optional vault_search).
 * Does not use the RAG chat transcript.
 */
export async function streamVoice(options: ChatStreamOptions): Promise<void> {
  const { signal, onEvent } = options

  let response: Response
  try {
    response = await fetch(`${BASE}/voice`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...authHeaders(),
      },
      body: JSON.stringify({
        message: options.message,
        history: options.history,
        profile: options.profile,
        thread_id: options.conversationId ?? 'voice',
        conversation_id: options.conversationId ?? null,
        approval_id: options.approvalId ?? null,
      }),
      signal,
    })
  } catch (error) {
    if (isAbort(error)) return
    onEvent({
      type: 'error',
      message: `Cannot reach the Jarvis backend. Is it running on port 8756? (${String(error)})`,
      code: 'backend_unreachable',
      recoverable: true,
    })
    return
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    const { code, message } = describeApiFailure(
      response.status,
      detail || '',
      response.statusText,
    )
    onEvent({
      type: 'error',
      message,
      code: code === 'http_error' ? `http_${response.status}` : code,
      recoverable: response.status === 429 || response.status < 500,
    })
    return
  }

  try {
    for await (const frame of readSse(response, signal)) {
      let parsed: StreamEvent
      try {
        parsed = JSON.parse(frame.data) as StreamEvent
      } catch {
        continue
      }
      onEvent(parsed)
    }
  } catch (error) {
    if (isAbort(error)) return
    onEvent({
      type: 'error',
      message: `Stream interrupted: ${String(error)}`,
      code: 'stream_error',
      recoverable: true,
    })
  }
}

export interface NotificationPayload {
  id: string
  kind: string
  title: string
  body: string
  /** Fired late because the app was closed or the machine was asleep. */
  missed: boolean
  fire_at: string
}

/** Cap for SSE reconnect backoff so 429 / flaky networks do not spin forever. */
const EVENTS_BACKOFF_CAP_MS = 30_000
/** Initial delay before the first reconnect after an events stream failure. */
const EVENTS_BACKOFF_BASE_MS = 1_000

/**
 * Subscribe to server-initiated events (timers firing).
 *
 * Uses ``fetch`` + SSE framing (same auth headers as chat) so Bearer /
 * ``X-Jarvis-Token`` are sent — ``EventSource`` cannot. On error or 429,
 * reconnects with exponential backoff (capped at ~30s). Returned unsubscribe
 * aborts the in-flight request and cancels any pending reconnect.
 */
export function subscribeEvents(
  onNotification: (payload: NotificationPayload) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  const controller = new AbortController()
  let attempt = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  /** Clear a pending reconnect timer without aborting the subscription. */
  const clearReconnect = () => {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  /** Schedule another connect attempt with exponential backoff up to the cap. */
  const scheduleReconnect = () => {
    if (controller.signal.aborted) return
    clearReconnect()
    const delay = Math.min(
      EVENTS_BACKOFF_CAP_MS,
      EVENTS_BACKOFF_BASE_MS * 2 ** attempt,
    )
    attempt += 1
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      void connect()
    }, delay)
  }

  /** Open (or reopen) the authenticated events stream until aborted. */
  const connect = async () => {
    if (controller.signal.aborted) return

    let response: Response
    try {
      response = await fetch(`${BASE}/events`, {
        method: 'GET',
        headers: {
          Accept: 'text/event-stream',
          ...authHeaders(),
        },
        signal: controller.signal,
      })
    } catch (error) {
      if (isAbort(error) || controller.signal.aborted) return
      onStatus?.(false)
      scheduleReconnect()
      return
    }

    if (!response.ok) {
      onStatus?.(false)
      // Drain the body so the connection can close cleanly before backoff.
      await response.text().catch(() => {})
      if (controller.signal.aborted) return
      scheduleReconnect()
      return
    }

    attempt = 0

    try {
      for await (const frame of readSse(response, controller.signal)) {
        if (frame.event === 'ready') {
          onStatus?.(true)
          continue
        }
        if (frame.event !== 'notification') continue
        try {
          onNotification(JSON.parse(frame.data) as NotificationPayload)
        } catch {
          /* malformed frame; nothing useful to do */
        }
      }
    } catch (error) {
      if (isAbort(error) || controller.signal.aborted) return
      onStatus?.(false)
      scheduleReconnect()
      return
    }

    if (controller.signal.aborted) return
    onStatus?.(false)
    scheduleReconnect()
  }

  void connect()

  return () => {
    clearReconnect()
    controller.abort()
  }
}

/** True when a fetch or stream failure is a user/system abort. */
function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}
