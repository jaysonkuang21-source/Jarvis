import { useEffect, useState } from 'react'
import { ChevronRight, ExternalLink, FileText, Loader2, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/controls'
import { api, type NoteResponse } from '@/lib/api/client'
import { cn } from '@/lib/utils'
import type { Citation } from '@/lib/api/types'

interface Props {
  citation: Citation | null
  onClose: () => void
}

const SOURCE_LABEL: Record<Citation['source'], string> = {
  graph: 'Graph',
  vector: 'Vector',
  visual: 'Visual',
  link: 'Wikilink',
}

/** Side panel that loads a cited note section and can open it in Obsidian. */
export function CitationsPanel({ citation, onClose }: Props) {
  const [note, setNote] = useState<NoteResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [showFullNote, setShowFullNote] = useState(false)

  useEffect(() => {
    if (!citation) {
      setNote(null)
      return
    }

    let cancelled = false
    setLoading(true)
    setFailed(null)
    setShowFullNote(false)

    api
      .readNote(citation.note_path, citation.char_start)
      .then((response) => !cancelled && setNote(response))
      .catch((error: Error) => !cancelled && setFailed(error.message))
      .finally(() => !cancelled && setLoading(false))

    return () => {
      cancelled = true
    }
  }, [citation])

  if (!citation) return null

  // The enclosing heading section, which is what "expand to parent" resolves to.
  const section = note
    ? note.content.slice(note.section_start, note.section_end).trim()
    : ''
  const body = showFullNote ? (note?.content ?? '') : section || citation.snippet

  /** Open the note via the backend plugin, falling back to an Obsidian URI. */
  async function openInObsidian(path: string) {
    try {
      const result = await api.openNote(path)
      // The plugin only answers while Obsidian is running; the URI does not
      // need it, so fall through rather than failing.
      if (!result.opened) window.location.href = result.uri
    } catch {
      /* nothing sensible left to try */
    }
  }

  return (
    <aside className="flex h-full w-[380px] shrink-0 flex-col border-l border-border bg-card">
      <header className="flex items-start gap-2 px-4 py-3">
        <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold" title={citation.note_title}>
            {citation.note_title}
          </h3>
          <p
            className="truncate font-mono text-[11px] text-muted-foreground"
            title={citation.note_path}
          >
            {citation.note_path}
          </p>
        </div>
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close panel">
          <X className="size-3.5" />
        </Button>
      </header>

      {citation.heading_path.length > 0 && (
        <nav className="flex flex-wrap items-center gap-0.5 px-4 pb-2 text-[11px] text-muted-foreground">
          {citation.heading_path.map((heading, index) => (
            <span key={index} className="flex items-center gap-0.5">
              {index > 0 && <ChevronRight className="size-3 opacity-50" />}
              <span className="truncate">{heading}</span>
            </span>
          ))}
        </nav>
      )}

      <div className="flex flex-wrap items-center gap-1.5 px-4 pb-3">
        <Badge variant="accent">{SOURCE_LABEL[citation.source]}</Badge>
        <Badge variant="outline">score {citation.score.toFixed(3)}</Badge>
        {citation.page !== null && <Badge variant="outline">page {citation.page}</Badge>}
      </div>

      <Separator />

      <div className="scrollbar-thin flex-1 overflow-y-auto px-4 py-3">
        {loading && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            Reading note...
          </div>
        )}

        {failed && (
          <div className="rounded-lg border border-warning/30 bg-warning/10 p-3">
            <p className="text-xs leading-relaxed text-warning">
              Could not read this note from disk. Showing the retrieved snippet instead.
            </p>
            <p className="mt-1 font-mono text-[10px] text-warning/70">{failed}</p>
          </div>
        )}

        {!loading && (
          <pre
            className={cn(
              'whitespace-pre-wrap break-words font-sans text-[13px] leading-relaxed',
              !body && 'italic text-muted-foreground',
            )}
          >
            {body || 'This note is empty.'}
          </pre>
        )}
      </div>

      <Separator />

      <footer className="flex items-center gap-2 px-4 py-3">
        <Button
          variant="outline"
          size="sm"
          className="flex-1"
          onClick={() => void openInObsidian(citation.note_path)}
        >
          <ExternalLink className="size-3.5" />
          Open in Obsidian
        </Button>
        {note && section && section !== note.content.trim() && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowFullNote((value) => !value)}
          >
            {showFullNote ? 'Section' : 'Full note'}
          </Button>
        )}
      </footer>
    </aside>
  )
}
