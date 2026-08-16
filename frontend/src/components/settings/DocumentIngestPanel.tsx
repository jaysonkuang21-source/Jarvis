import { Eye, FileUp, Loader2, RefreshCw, Trash2 } from 'lucide-react'
import { useEffect, useState, type ChangeEvent } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input, Label, Switch, Textarea } from '@/components/ui/controls'
import {
  api,
  type DocumentChunkPreview,
  type IndexedDocumentSummary,
} from '@/lib/api/client'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/stores/app'
import { useProfileStore } from '@/stores/profile'
import { SettingsSection } from './Field'

/** Add documents and inspect indexed chunks across app sessions. */
export function DocumentIngestPanel({
  onIndexed,
}: {
  /** Called after a successful write when the user opted to reindex. */
  onIndexed?: () => void
}) {
  const pushToast = useAppStore((s) => s.pushToast)
  const startReindex = useProfileStore((s) => s.startReindex)
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [reindexAfter, setReindexAfter] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [indexedDocs, setIndexedDocs] = useState<IndexedDocumentSummary[]>([])
  const [docsLoading, setDocsLoading] = useState(false)
  const [inspectPath, setInspectPath] = useState<string | null>(null)
  const [chunkTotal, setChunkTotal] = useState(0)
  const [chunks, setChunks] = useState<DocumentChunkPreview[]>([])
  const [chunksLoading, setChunksLoading] = useState(false)
  const [showCount, setShowCount] = useState(10)
  const [charsPerChunk, setCharsPerChunk] = useState(200)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /** Load every indexed document from Postgres (survives UI restarts). */
  async function refreshIndexedDocuments() {
    setDocsLoading(true)
    try {
      const result = await api.listIndexedDocuments()
      setIndexedDocs(result.documents)
      setError(null)
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : 'Could not load indexed documents',
      )
    } finally {
      setDocsLoading(false)
    }
  }

  /** Persist written paths and optionally kick off a vault reindex. */
  async function finish(paths: string[]) {
    pushToast(
      'INGEST',
      paths.length === 1
        ? `Saved ${paths[0]}`
        : `Saved ${paths.length} notes to Inbox/`,
    )
    if (reindexAfter) {
      try {
        await startReindex(false)
        onIndexed?.()
        pushToast('INDEX', 'Reindex started for new documents')
      } catch (err) {
        setError(
          err instanceof Error
            ? `Saved, but reindex failed: ${err.message}`
            : 'Saved, but reindex failed',
        )
      }
    } else {
      await refreshIndexedDocuments()
    }
  }

  /** Upload one or more files of any type into the vault Inbox. */
  async function onUpload(event: ChangeEvent<HTMLInputElement>) {
    const fileList = event.target.files
    if (!fileList?.length) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.ingestNoteFiles(Array.from(fileList))
      await finish(result.paths)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setBusy(false)
      event.target.value = ''
    }
  }

  /** Paste markdown/plain text as an Inbox note. */
  async function onPaste() {
    if (!content.trim()) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.ingestNotePaste({
        content,
        title: title.trim() || undefined,
      })
      await finish(result.paths)
      setContent('')
      setTitle('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save note')
    } finally {
      setBusy(false)
    }
  }

  /** Remove one ingested document from the index and vault. */
  async function onRemove(notePath: string) {
    const trashVault = window.confirm(
      `Remove "${notePath}" from the search index and move vault files to trash?`,
    )
    if (!trashVault) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.deleteIndexedDocument(notePath, true)
      setIndexedDocs((docs) => docs.filter((d) => d.path !== notePath))
      if (inspectPath === notePath) {
        setInspectPath(null)
        setChunks([])
        setChunkTotal(0)
      }
      pushToast(
        'INGEST',
        result.removed_from_index
          ? `Removed ${notePath} from index`
          : `${notePath} was not in the index`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove document')
    } finally {
      setBusy(false)
    }
  }

  /** Load chunk inventory for one path after reindex settles. */
  async function loadChunks(notePath: string) {
    setInspectPath(notePath)
    setExpandedId(null)
    setChunksLoading(true)
    setError(null)
    try {
      const result = await api.listDocumentChunks(notePath)
      setChunkTotal(result.total)
      setChunks(result.chunks)
      setShowCount((n) => Math.min(Math.max(1, n), Math.max(1, result.total || 1)))
      if (result.total === 0 && indexStatus?.indexing) {
        setError('Reindex still running — chunks will appear when it finishes.')
      }
    } catch (err) {
      setChunks([])
      setChunkTotal(0)
      setError(err instanceof Error ? err.message : 'Could not load chunks')
    } finally {
      setChunksLoading(false)
    }
  }

  useEffect(() => {
    void refreshIndexedDocuments()
    // Load once on mount; later refreshes are explicit or post-reindex.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount only
  }, [])

  useEffect(() => {
    if (indexStatus?.indexing) return
    void refreshIndexedDocuments()
    if (!inspectPath) return
    void loadChunks(inspectPath)
    // Intentionally re-fetch when indexing flips to idle.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- indexing + inspectPath
  }, [indexStatus?.indexing])

  const visibleChunks = chunks.slice(0, showCount)

  return (
    <SettingsSection id="ingestion-add" title="Add documents">
      <p className="pb-3 text-xs leading-relaxed text-muted-foreground">
        Upload any file type. Jarvis stores the original under{' '}
        <code className="text-[11px]">Inbox/files/</code>, writes a searchable note,
        and tags the preferred retriever (
        <code className="text-[11px]">text-hybrid</code>,{' '}
        <code className="text-[11px]">visual</code>, or{' '}
        <code className="text-[11px]">binary-meta</code>). PDFs and Word (
        <code className="text-[11px]">.docx</code>) get text extraction; images use OCR
        when Tesseract is installed. Reindex to digest. Indexed documents below stay
        available across sessions — View chunks to inspect quality anytime.
      </p>

      <div className="space-y-3 pb-3">
        <div>
          <Label className="text-[13px]">Upload files</Label>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Input
              type="file"
              accept="*/*"
              multiple
              disabled={busy}
              className="max-w-md cursor-pointer text-xs file:mr-3 file:rounded file:border-0 file:bg-secondary file:px-2 file:py-1 file:text-xs"
              onChange={(event) => void onUpload(event)}
            />
          </div>
        </div>

        <div className="border-t border-border pt-3">
          <Label className="text-[13px]">Or paste a note</Label>
          <Input
            className="mt-1.5"
            value={title}
            disabled={busy}
            placeholder="Title (optional)"
            onChange={(event) => setTitle(event.target.value)}
          />
          <Textarea
            className="mt-2 min-h-28"
            value={content}
            disabled={busy}
            placeholder="Paste markdown or plain text…"
            onChange={(event) => setContent(event.target.value)}
          />
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <Switch
                checked={reindexAfter}
                onCheckedChange={setReindexAfter}
                disabled={busy}
              />
              Reindex after adding
            </label>
            <Button size="sm" disabled={busy || !content.trim()} onClick={() => void onPaste()}>
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <FileUp className="size-3.5" />
              )}
              Add to vault
            </Button>
          </div>
        </div>
      </div>

      {error && (
        <p className="pb-2 text-xs text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="border-t border-border pt-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-medium">Indexed documents</h4>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-xs"
            disabled={docsLoading || busy}
            onClick={() => void refreshIndexedDocuments()}
          >
            {docsLoading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            Refresh
          </Button>
        </div>
        <p className="pb-2 text-xs text-muted-foreground">
          {docsLoading && indexedDocs.length === 0
            ? 'Loading index…'
            : indexedDocs.length === 0
              ? 'No documents in the index yet. Ingest and reindex to populate this list.'
              : `${indexedDocs.length} document${indexedDocs.length === 1 ? '' : 's'} in Postgres`}
        </p>
        {indexedDocs.length > 0 && (
          <div className="flex max-h-56 flex-col gap-2 overflow-y-auto pb-3">
            {indexedDocs.map((doc) => (
              <div
                key={doc.path}
                className="flex flex-wrap items-center gap-2"
              >
                <Badge
                  variant={inspectPath === doc.path ? 'accent' : 'outline'}
                  className="max-w-full"
                >
                  <span className="truncate">
                    {doc.path}
                    {doc.chunk_count > 0 ? ` · ${doc.chunk_count} chunks` : ''}
                  </span>
                </Badge>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs"
                  disabled={busy || chunksLoading}
                  onClick={() => void loadChunks(doc.path)}
                >
                  <Eye className="size-3.5" />
                  View chunks
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                  disabled={busy}
                  onClick={() => void onRemove(doc.path)}
                >
                  <Trash2 className="size-3.5" />
                  Remove
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {inspectPath && (
        <div className="mt-2 space-y-3 border-t border-border pt-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h4 className="text-sm font-medium">
              Chunks for{' '}
              <code className="text-xs text-muted-foreground">{inspectPath}</code>
            </h4>
            <p className="text-xs text-muted-foreground">
              {chunksLoading
                ? 'Loading…'
                : indexStatus?.indexing
                  ? 'Pending reindex…'
                  : `${chunkTotal} chunk${chunkTotal === 1 ? '' : 's'} created`}
            </p>
          </div>

          {!chunksLoading && chunkTotal > 0 && (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex flex-col gap-1 text-xs">
                  Chunks to show ({showCount} of {chunkTotal})
                  <input
                    type="range"
                    min={1}
                    max={Math.max(1, Math.min(chunkTotal, chunks.length || chunkTotal))}
                    value={Math.min(showCount, Math.max(1, chunkTotal))}
                    onChange={(e) => setShowCount(Number(e.target.value))}
                    className="w-full"
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  Characters per chunk ({charsPerChunk})
                  <input
                    type="range"
                    min={40}
                    max={2000}
                    step={20}
                    value={charsPerChunk}
                    onChange={(e) => setCharsPerChunk(Number(e.target.value))}
                    className="w-full"
                  />
                </label>
              </div>

              <ul className="flex max-h-80 flex-col gap-1.5 overflow-y-auto">
                {visibleChunks.map((chunk) => {
                  const full = expandedId === chunk.chunk_id
                  const body =
                    full || chunk.text.length <= charsPerChunk
                      ? chunk.text
                      : `${chunk.text.slice(0, charsPerChunk)}…`
                  const heading =
                    chunk.heading_path.length > 0
                      ? chunk.heading_path.join(' › ')
                      : '(no heading)'
                  const tags = chunk.tags ?? []
                  const wikilinks = chunk.wikilinks ?? []
                  const entities = chunk.entities ?? []
                  const canExpand = chunk.text.length > charsPerChunk
                  return (
                    <li key={chunk.chunk_id}>
                      <button
                        type="button"
                        disabled={!canExpand}
                        onClick={() => {
                          if (!canExpand) return
                          setExpandedId((id) =>
                            id === chunk.chunk_id ? null : chunk.chunk_id,
                          )
                        }}
                        className={cn(
                          'flex w-full flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors',
                          full
                            ? 'border-primary bg-accent text-accent-foreground'
                            : 'border-border hover:bg-muted',
                          !canExpand && 'cursor-default hover:bg-transparent',
                        )}
                      >
                        <span className="text-[13px] font-medium">
                          {chunk.chunk_id}
                          <span className="ml-1.5 font-normal text-muted-foreground">
                            {heading}
                          </span>
                        </span>
                        <span className="text-[11px] leading-snug text-muted-foreground">
                          [{chunk.char_start}–{chunk.char_end}]
                          {chunk.note_title ? ` · ${chunk.note_title}` : ''}
                          {canExpand ? (full ? ' · Collapse' : ' · Expand') : ''}
                        </span>
                        {(tags.length > 0 ||
                          wikilinks.length > 0 ||
                          entities.length > 0) && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {tags.map((tag) => (
                              <Badge
                                key={`tag-${tag}`}
                                variant="outline"
                                className="h-5 px-1.5 text-[10px]"
                              >
                                #{tag}
                              </Badge>
                            ))}
                            {wikilinks.map((link) => (
                              <Badge
                                key={`link-${link}`}
                                variant="secondary"
                                className="h-5 px-1.5 font-mono text-[10px]"
                              >
                                [[{link}]]
                              </Badge>
                            ))}
                            {entities.map((name) => (
                              <Badge
                                key={`ent-${name}`}
                                variant="outline"
                                className="h-5 px-1.5 text-[10px]"
                              >
                                {name}
                              </Badge>
                            ))}
                          </div>
                        )}
                        {tags.length === 0 &&
                          wikilinks.length === 0 &&
                          entities.length === 0 && (
                            <span className="text-[11px] italic leading-snug text-muted-foreground">
                              No tags, wikilinks, or entities on this chunk.
                            </span>
                          )}
                        <pre className="mt-1 w-full whitespace-pre-wrap break-words font-sans text-[12px] leading-relaxed text-foreground">
                          {body}
                        </pre>
                      </button>
                    </li>
                  )
                })}
              </ul>
            </>
          )}

          {!chunksLoading && chunkTotal === 0 && !indexStatus?.indexing && (
            <p className="text-xs text-muted-foreground">
              No chunks yet for this path. Reindex after ingest, then view again.
            </p>
          )}
        </div>
      )}
    </SettingsSection>
  )
}
