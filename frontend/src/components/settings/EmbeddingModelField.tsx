import { useState, type ReactNode } from 'react'
import { AlertTriangle, Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useProfileStore } from '@/stores/profile'
import type { ModelInfo, Provider } from '@/lib/api/types'
import { commitEmbeddingModelChange } from '@/lib/embeddingChange'
import { RoleRecommendControl } from './RoleRecommendControl'

/**
 * Embedding model is not a peer of the chat model picker.
 *
 * Chat models swap freely. Embedding width is pinned to the vector schema:
 * changing it invalidates every stored vector and forces a full re-index.
 * After an index exists (or a model is already chosen), the picker stays
 * locked unless the user explicitly opens the advanced change flow.
 */
export function EmbeddingModelField({
  models,
  showRecommend = true,
}: {
  models: ModelInfo[]
  /** When true, show the per-role Recommend control under the select. */
  showRecommend?: boolean
}) {
  const { profile, indexStatus, update } = useProfileStore()
  const [pending, setPending] = useState<ModelInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const indexed = (indexStatus?.indexed_notes ?? 0) > 0
  const pinnedTo = indexStatus?.embedding_model
  const locked = indexed || Boolean(pinnedTo) || Boolean(profile.embedding_model)

  /** Select an embedding model, confirming first if an index already exists. */
  function choose(id: string, provider: Provider = 'ollama') {
    if (id === profile.embedding_model) return
    const model =
      models.find((m) => m.id === id) ??
      ({
        id,
        provider,
        label: id,
        context_window: 8192,
        supports_vision: false,
        supports_tools: false,
        is_embedding: true,
        dimensions: null,
        available: true,
        unavailable_reason: null,
        parameter_b: null,
        est_vram_mb: null,
        size_bytes: null,
        hf_id: null,
        role_scores: null,
        tier: null,
      } satisfies ModelInfo)
    if (indexed || pinnedTo) setPending(model)
    else apply(model)
  }

  /** Commit the embedding model change into the draft profile. */
  function apply(model: ModelInfo) {
    update({ embedding_model: model.id, embedding_provider: model.provider })
    setPending(null)
    setAdvancedOpen(false)
  }

  /** Persist the new embedding model and start a full reindex. */
  async function applyAndReindex(model: ModelInfo) {
    setBusy(true)
    try {
      const ok = await commitEmbeddingModelChange(
        { id: model.id, provider: model.provider },
        { skipConfirm: true },
      )
      if (ok) {
        setPending(null)
        setAdvancedOpen(false)
      }
    } catch (err) {
      window.alert(err instanceof Error ? err.message : 'Could not save embedding model')
    } finally {
      setBusy(false)
    }
  }

  const recommend: ReactNode =
    showRecommend && !locked ? (
      <RoleRecommendControl
        role="embedding"
        onApplyEmbedding={(id, provider) => choose(id, provider)}
      />
    ) : null

  return (
    <>
      <Select
        value={profile.embedding_model}
        disabled={locked}
        onValueChange={(id) => {
          const model = models.find((m) => m.id === id)
          choose(id, model?.provider ?? 'ollama')
        }}
      >
        <SelectTrigger>
          <SelectValue placeholder="Select an embedding model" />
        </SelectTrigger>
        <SelectContent>
          {models.length === 0 && (
            <div className="px-2 py-3 text-xs text-muted-foreground">
              No embedding models found.
            </div>
          )}
          {models.map((model) => (
            <SelectItem key={model.id} value={model.id} disabled={!model.available}>
              <span className="flex flex-col gap-0.5">
                <span>{model.label}</span>
                <span className="text-[10px] text-muted-foreground">
                  {model.provider}
                  {model.dimensions ? ` | ${model.dimensions} dims` : ''}
                  {!model.available && ` | ${model.unavailable_reason}`}
                </span>
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {locked && (
        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <Lock className="mt-0.5 size-3.5 shrink-0" />
          <span>
            Embedding is pinned
            {pinnedTo ? ` to ${pinnedTo}` : ` to ${profile.embedding_model}`} for the
            vector index. Chat and utility models may change freely; this one
            should not.
          </span>
        </p>
      )}

      {locked && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-1"
          onClick={() => setAdvancedOpen(true)}
        >
          Advanced: change embedding (re-index)
        </Button>
      )}

      {recommend}

      {pinnedTo && pinnedTo !== profile.embedding_model && (
        <p className="text-xs text-warning">
          The existing index was built with {pinnedTo}. Searching with a different
          embedding model will not work until you re-index.
        </p>
      )}

      <Dialog
        open={advancedOpen && pending === null}
        onOpenChange={(open) => !open && setAdvancedOpen(false)}
      >
        <DialogContent showClose={false}>
          <DialogHeader>
            <div className="mb-1 flex size-9 items-center justify-center rounded-lg bg-warning/15">
              <AlertTriangle className="size-4.5 text-warning" />
            </div>
            <DialogTitle>Change embedding model?</DialogTitle>
            <DialogDescription>
              Pick a new embedding model below. Wrong dimensions make every stored
              vector unusable and require a full re-index
              {indexed ? ` of ${indexStatus?.indexed_notes ?? 0} notes` : ''}.
            </DialogDescription>
          </DialogHeader>

          <Select
            value={profile.embedding_model}
            onValueChange={(id) => {
              const model = models.find((m) => m.id === id)
              choose(id, model?.provider ?? 'ollama')
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder="Select an embedding model" />
            </SelectTrigger>
            <SelectContent>
              {models.map((model) => (
                <SelectItem key={model.id} value={model.id} disabled={!model.available}>
                  {model.label}
                  {model.dimensions ? ` (${model.dimensions}d)` : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <DialogFooter>
            <Button variant="outline" onClick={() => setAdvancedOpen(false)} disabled={busy}>
              Keep {profile.embedding_model}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent showClose={false}>
          <DialogHeader>
            <div className="mb-1 flex size-9 items-center justify-center rounded-lg bg-warning/15">
              <AlertTriangle className="size-4.5 text-warning" />
            </div>
            <DialogTitle>This invalidates your index</DialogTitle>
            <DialogDescription>
              Switching from {profile.embedding_model} to {pending?.id} makes every
              stored vector unusable. The two models produce different dimensions,
              so nothing can be reused and all{' '}
              {indexStatus?.indexed_notes ?? 0} indexed notes must be embedded again.
            </DialogDescription>
          </DialogHeader>

          <p className="text-xs leading-relaxed text-muted-foreground">
            Your notes are untouched; only the derived index is discarded. Global
            search keeps working meanwhile since it never uses embeddings.
          </p>

          <DialogFooter>
            <Button variant="outline" onClick={() => setPending(null)} disabled={busy}>
              Keep {profile.embedding_model}
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => pending && void applyAndReindex(pending)}
            >
              {busy ? 'Saving…' : 'Change and re-index'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
