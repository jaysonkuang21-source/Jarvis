/**
 * Shared embedding model change path: confirm → draft update → save → reindex.
 *
 * Chat models may swap freely. Embedding width is pinned to the vector schema,
 * so Recommend/Suggest Apply must use this (or EmbeddingModelField’s dialogs),
 * never a plain profile.update().
 */

import type { Provider } from '@/lib/api/types'
import { useProfileStore } from '@/stores/profile'

export interface EmbeddingApplyTarget {
  id: string
  provider: Provider
}

/**
 * Confirm (when an index exists), persist embedding model/provider, and reindex.
 * Returns false when the user cancels; throws if save fails.
 */
export async function commitEmbeddingModelChange(
  target: EmbeddingApplyTarget,
  opts?: { skipConfirm?: boolean },
): Promise<boolean> {
  const { profile, indexStatus, update, save, startReindex } = useProfileStore.getState()
  if (target.id === profile.embedding_model && target.provider === profile.embedding_provider) {
    return true
  }

  const indexedNotes = indexStatus?.indexed_notes ?? 0
  const indexed = indexedNotes > 0
  const pinnedTo = indexStatus?.embedding_model
  if (!opts?.skipConfirm && (indexed || pinnedTo)) {
    const ok = window.confirm(
      `Switch embedding to ${target.id}? This invalidates the existing index` +
        (indexed ? ` (${indexedNotes} notes)` : '') +
        ` and starts a full re-index. Continue?`,
    )
    if (!ok) return false
  }

  update({ embedding_model: target.id, embedding_provider: target.provider })
  await save()
  try {
    await startReindex()
  } catch (err) {
    window.alert(
      err instanceof Error
        ? `Saved, but reindex failed: ${err.message}`
        : 'Saved, but reindex failed. Restart the backend if /api/index/reindex is missing.',
    )
  }
  return true
}
