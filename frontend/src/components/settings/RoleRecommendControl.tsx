/**
 * Per-role Recommend / Apply controls for Settings model pickers.
 */

import { useState } from 'react'
import { Loader2, Sparkles } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api/client'
import { commitEmbeddingModelChange } from '@/lib/embeddingChange'
import type {
  ModelRecommendation,
  Provider,
  RecommendResponse,
} from '@/lib/api/types'
import { useProfileStore } from '@/stores/profile'

export type RecommendRole =
  | 'chat'
  | 'voice'
  | 'embedding'
  | 'chunk_decision'
  | 'extraction'
  | 'rerank'

const ROLE_LABEL: Record<RecommendRole, string> = {
  chat: 'Chat',
  voice: 'Voice',
  embedding: 'Embedding',
  chunk_decision: 'Chunk decision',
  extraction: 'Extraction',
  rerank: 'Rerank',
}

type ProfilePatch = {
  modelKey:
    | 'chat_model'
    | 'voice_model'
    | 'embedding_model'
    | 'chunk_decision_model'
    | 'extraction_model'
    | 'rerank_model'
  providerKey:
    | 'chat_provider'
    | 'voice_provider'
    | 'embedding_provider'
    | 'chunk_decision_provider'
    | 'extraction_provider'
    | 'rerank_provider'
}

const ROLE_PROFILE_FIELDS: Record<RecommendRole, ProfilePatch> = {
  chat: { modelKey: 'chat_model', providerKey: 'chat_provider' },
  voice: { modelKey: 'voice_model', providerKey: 'voice_provider' },
  embedding: { modelKey: 'embedding_model', providerKey: 'embedding_provider' },
  chunk_decision: {
    modelKey: 'chunk_decision_model',
    providerKey: 'chunk_decision_provider',
  },
  extraction: {
    modelKey: 'extraction_model',
    providerKey: 'extraction_provider',
  },
  rerank: { modelKey: 'rerank_model', providerKey: 'rerank_provider' },
}

/** Compact Recommend + Apply next to a role’s model select. */
export function RoleRecommendControl({
  role,
  onApplyEmbedding,
}: {
  role: RecommendRole
  /** Embedding applies go through EmbeddingModelField’s confirm path when provided. */
  onApplyEmbedding?: (id: string, provider: Provider) => void
}) {
  const profile = useProfileStore((s) => s.profile)
  const update = useProfileStore((s) => s.update)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pick, setPick] = useState<ModelRecommendation | null>(null)
  const [alts, setAlts] = useState<ModelRecommendation[]>([])
  const [showAlts, setShowAlts] = useState(false)

  /** Call the recommend API for this role and keep the top pick + alternatives. */
  async function recommend() {
    setBusy(true)
    setError(null)
    try {
      const response = await api.recommendModels({
        roles: [role],
        online: Boolean(profile.model_metrics_online),
        profile,
      })
      const row = response.roles.find((r) => r.role === role)
      const list = row?.recommendations ?? []
      setPick(list[0] ?? null)
      setAlts(list.slice(1))
      setShowAlts(false)
      if (!list[0]) setError('No candidates for this role.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Recommend failed')
      setPick(null)
      setAlts([])
    } finally {
      setBusy(false)
    }
  }

  /** Patch the draft profile — embedding always uses the locked save+reindex path. */
  async function apply(rec: ModelRecommendation) {
    if (rec.disabled_reason) return
    if (role === 'embedding') {
      if (onApplyEmbedding) {
        onApplyEmbedding(rec.id, rec.provider)
        return
      }
      setBusy(true)
      setError(null)
      try {
        await commitEmbeddingModelChange({ id: rec.id, provider: rec.provider })
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not apply embedding model')
      } finally {
        setBusy(false)
      }
      return
    }
    const fields = ROLE_PROFILE_FIELDS[role]
    update({
      [fields.modelKey]: rec.id,
      [fields.providerKey]: rec.provider,
    })
  }

  return (
    <div className="mt-2 flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => void recommend()}
        >
          {busy ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Sparkles className="size-3.5" />
          )}
          Recommend
        </Button>
        {pick && (
          <>
            <span className="text-xs text-muted-foreground">
              {pick.id}
              {pick.needs_pull ? ' (pull needed)' : ''}
              {!pick.fits ? ' (may not fit)' : ''}
            </span>
            <Button
              type="button"
              size="sm"
              disabled={Boolean(pick.disabled_reason) || busy}
              title={pick.disabled_reason || undefined}
              onClick={() => void apply(pick)}
            >
              Apply
            </Button>
            {alts.length > 0 && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowAlts((v) => !v)}
              >
                {showAlts ? 'Hide alternatives' : 'Show alternatives'}
              </Button>
            )}
          </>
        )}
      </div>
      {pick && pick.reasons.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {pick.reasons.slice(0, 4).map((reason) => (
            <Badge key={reason} variant="outline" className="font-normal">
              {reason}
            </Badge>
          ))}
        </div>
      )}
      {showAlts &&
        alts.map((alt) => (
          <div key={alt.id} className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-mono">{alt.id}</span>
            <span className="text-muted-foreground">{alt.score.toFixed(2)}</span>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={Boolean(alt.disabled_reason) || busy}
              onClick={() => void apply(alt)}
            >
              Apply
            </Button>
          </div>
        ))}
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

/** Suggested-for-this-machine panel covering every recommend role. */
export function SuggestedModelsPanel() {
  const profile = useProfileStore((s) => s.profile)
  const update = useProfileStore((s) => s.update)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [response, setResponse] = useState<RecommendResponse | null>(null)

  /** Refresh recommendations for every supported role. */
  async function refreshAll() {
    setBusy(true)
    setError(null)
    try {
      const next = await api.recommendModels({
        roles: null,
        online: Boolean(profile.model_metrics_online),
        profile,
      })
      setResponse(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Recommend failed')
    } finally {
      setBusy(false)
    }
  }

  /** Apply a top pick; embedding uses the locked save+reindex helper. */
  async function applyTop(role: RecommendRole, rec: ModelRecommendation) {
    if (rec.disabled_reason) return
    if (role === 'embedding') {
      setBusy(true)
      setError(null)
      try {
        await commitEmbeddingModelChange({ id: rec.id, provider: rec.provider })
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not apply embedding model')
      } finally {
        setBusy(false)
      }
      return
    }
    const fields = ROLE_PROFILE_FIELDS[role]
    update({
      [fields.modelKey]: rec.id,
      [fields.providerKey]: rec.provider,
    })
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => void refreshAll()}
        >
          {busy ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Sparkles className="size-3.5" />
          )}
          Suggest for this machine
        </Button>
        {response?.metrics_degraded && (
          <Badge variant="warning">online metrics degraded</Badge>
        )}
      </div>
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
      {response?.roles.map((row) => {
        const top = row.recommendations[0]
        if (!top) return null
        const role = row.role as RecommendRole
        const embeddingHint =
          role === 'embedding'
            ? 'Saves and re-indexes (same path as Advanced embedding change)'
            : undefined
        return (
          <div
            key={row.role}
            className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 py-2 text-xs last:border-0"
          >
            <div className="min-w-0">
              <p className="font-medium">{ROLE_LABEL[role] ?? row.role}</p>
              <p className="truncate text-muted-foreground">
                {top.id} · score {top.score.toFixed(2)}
                {top.needs_pull ? ' · pull needed' : ''}
              </p>
              <div className="mt-1 flex flex-wrap gap-1">
                {top.reasons.slice(0, 3).map((reason) => (
                  <Badge key={reason} variant="outline" className="font-normal">
                    {reason}
                  </Badge>
                ))}
              </div>
            </div>
            <Button
              type="button"
              size="sm"
              disabled={Boolean(top.disabled_reason) || busy}
              title={top.disabled_reason || embeddingHint}
              onClick={() => void applyTop(role, top)}
            >
              Apply
            </Button>
          </div>
        )
      })}
    </div>
  )
}
