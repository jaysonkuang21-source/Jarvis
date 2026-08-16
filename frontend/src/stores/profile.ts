import { create } from 'zustand'
import { api } from '@/lib/api/client'
import type {
  IndexStatus,
  OptionsResponse,
  Profile,
  ProfileValidation,
  ValidationIssue,
} from '@/lib/api/types'
import { notify } from '@/lib/notify'
import { useAppStore } from '@/stores/app'

/** Short summary for indexing-complete toasts. */
function statusNotes(status: IndexStatus | null): string {
  if (!status) return ''
  const parts: string[] = []
  if (status.indexed_notes != null && status.total_notes != null) {
    parts.push(`${status.indexed_notes}/${status.total_notes} notes`)
  }
  if (status.entities) parts.push(`${status.entities} entities`)
  if (status.communities) parts.push(`${status.communities} communities`)
  return parts.length ? ` · ${parts.join(', ')}` : ''
}

export const DEFAULT_PROFILE: Profile = {
  id: 'default',
  name: 'Default',
  chat_model: 'qwen3.5:9b',
  chat_provider: 'ollama',
  voice_model: 'qwen3.5:2b',
  voice_provider: 'ollama',
  embedding_model: 'qwen3-embedding:8b',
  embedding_provider: 'ollama',
  rag_mode: 'regular',
  query_mode: 'local',
  ingest_mode: 'regular',
  ingest_effort: 'manual',
  chunker: 'recursive',
  chunk_size: 700,
  chunk_overlap: 100,
  chunk_decision_model: 'qwen3.5:2b',
  chunk_decision_provider: 'ollama',
  extraction_model: 'qwen3.5:2b',
  extraction_provider: 'ollama',
  rerank_model: 'qwen3.5:2b',
  rerank_provider: 'ollama',
  agentic_max_iters: 3,
  rrf_k: 60,
  hybrid_vector_top_k: 20,
  hybrid_keyword_top_k: 20,
  prepend_note_context: true,
  expand_to_parent: true,
  community_level: 2,
  max_context_tokens: 8000,
  top_k: 10,
  tracing_enabled: false,
  model_metrics_online: false,
}

/** Why a specific choice would be rejected, keyed `field:value`. */
export type DisabledOptions = Record<string, string>

interface ProfileState {
  profile: Profile
  options: OptionsResponse | null
  validation: ProfileValidation
  disabled: DisabledOptions
  indexStatus: IndexStatus | null
  loading: boolean
  dirty: boolean
  /** Human-readable bootstrap failure, if profile/options load degraded. */
  loadError: string | null

  load: () => Promise<void>
  refreshModels: () => Promise<void>
  refreshIndexStatus: () => Promise<void>
  startReindex: (force?: boolean) => Promise<void>
  update: (patch: Partial<Profile>) => void
  save: () => Promise<void>
  issueFor: (field: string) => ValidationIssue[]
  disabledReason: (field: string, value: string) => string | undefined
}

const EMPTY_VALIDATION: ProfileValidation = { valid: true, issues: [] }

let validateController: AbortController | null = null
let validateTimer: ReturnType<typeof setTimeout> | null = null

/**
 * Merge a sparse/older API profile onto defaults so Settings sliders never
 * receive undefined (older backends omit fields like rrf_k / ingest_effort).
 * Explicit null/undefined from JSON must not wipe defaults.
 */
export function coerceProfile(raw: Partial<Profile> | null | undefined): Profile {
  const patch: Partial<Profile> = {}
  if (raw) {
    for (const [key, value] of Object.entries(raw) as [keyof Profile, Profile[keyof Profile]][]) {
      if (value !== null && value !== undefined) {
        ;(patch as Record<string, unknown>)[key] = value
      }
    }
  }
  return { ...DEFAULT_PROFILE, ...patch }
}

/** Ensure validation always has a boolean + issues array for selectors/UI. */
export function coerceValidation(
  raw: Partial<ProfileValidation> | null | undefined,
): ProfileValidation {
  return {
    valid: raw?.valid ?? true,
    issues: Array.isArray(raw?.issues) ? raw.issues : [],
  }
}

export const useProfileStore = create<ProfileState>()((set, get) => ({
  profile: DEFAULT_PROFILE,
  options: null,
  validation: EMPTY_VALIDATION,
  disabled: {},
  indexStatus: null,
  loading: true,
  dirty: false,
  loadError: null,

  /** Load profile, model options, and index status from the backend. */
  load: async () => {
    set({ loading: true, loadError: null })
    let profileFailed = false
    let optionsFailed = false
    const [rawProfile, options, indexStatus] = await Promise.all([
      api.getProfile().catch(() => {
        profileFailed = true
        return DEFAULT_PROFILE
      }),
      api.options().catch(() => {
        optionsFailed = true
        return null
      }),
      api.indexStatus().catch(() => null),
    ])
    const profile = coerceProfile(rawProfile)
    const loadError =
      profileFailed && optionsFailed
        ? 'Could not reach the backend for profile settings. Showing local defaults — retry when the API is up.'
        : profileFailed
          ? 'Could not load the saved profile. Showing local defaults.'
          : optionsFailed
            ? 'Could not load model/option catalogs. Using built-in fallbacks.'
            : null
    set({ profile, options, indexStatus, loading: false, dirty: false, loadError })
    void revalidate(profile, set)
  },

  /** Force a model list rescan and revalidate the current profile. */
  refreshModels: async () => {
    const options = await api.options(true).catch(() => null)
    if (options) set({ options })
    void revalidate(get().profile, set)
  },

  /** Refresh vault index readiness badges. */
  refreshIndexStatus: async () => {
    const indexStatus = await api.indexStatus().catch(() => null)
    if (indexStatus) set({ indexStatus })
  },

  /** Kick off a background reindex and poll status while it runs. */
  startReindex: async (force = false) => {
    const indexStatus = await api.reindex(force)
    set({ indexStatus })
    const poll = async () => {
      // Up to ~15 minutes (450 × 2s) — local Ollama embed+extraction is slow.
      let sawIdle = false
      for (let i = 0; i < 450; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        const status = await api.indexStatus().catch(() => null)
        if (!status) return
        set({ indexStatus: status })
        if (!status.indexing) {
          sawIdle = true
          break
        }
      }
      if (!sawIdle) {
        useAppStore.getState().pushToast(
          'INDEX',
          'Indexing still running after 15 minutes — check Settings → Index',
        )
        return
      }
      const finalStatus = get().indexStatus
      const notes = statusNotes(finalStatus)
      useAppStore.getState().pushToast('INDEX', `Indexing finished${notes}`)
      void notify({
        title: 'Jarvis indexing finished',
        body: notes
          ? notes.replace(/^ · /, '')
          : 'Vault reindex is complete.',
      })
    }
    void poll()
  },

  /** Patch the draft profile and debounce a compatibility revalidation. */
  update: (patch) => {
    const profile = { ...get().profile, ...patch }
    set({ profile, dirty: true })

    // Debounced so dragging a slider does not fire a request per pixel.
    if (validateTimer) clearTimeout(validateTimer)
    validateTimer = setTimeout(() => void revalidate(profile, set), 180)
  },

  /** Persist the draft profile to the backend. */
  save: async () => {
    const saved = await api.saveProfile(get().profile)
    set({ profile: coerceProfile(saved), dirty: false })
  },

  /** Return validation issues that apply to a single field (imperative helper). */
  issueFor: (field) => {
    const issues = get().validation.issues
    return Array.isArray(issues) ? issues.filter((issue) => issue.field === field) : []
  },

  /** Reason a field value is disabled, if the compatibility matrix says so. */
  disabledReason: (field, value) => get().disabled[`${field}:${value}`],
}))

/** Fetch the profile matrix and update validation/disabled option state. */
async function revalidate(
  profile: Profile,
  set: (partial: Partial<ProfileState>) => void,
) {
  validateController?.abort()
  validateController = new AbortController()
  try {
    const matrix = await api.profileMatrix(profile, validateController.signal)
    const disabled: DisabledOptions = {}
    const options = Array.isArray(matrix.options) ? matrix.options : []
    for (const option of options) {
      if (!option.valid) disabled[`${option.field}:${option.value}`] = option.reason
    }
    set({ validation: coerceValidation(matrix.current), disabled })
  } catch {
    // A superseded or failed check must not wipe the last good result.
  }
}
