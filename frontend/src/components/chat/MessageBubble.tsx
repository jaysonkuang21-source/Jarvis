import { AlertTriangle, Database, FileText, RotateCcw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Markdown } from './Markdown'
import { cn, formatDuration } from '@/lib/utils'
import type { Citation } from '@/lib/api/types'
import type { Message } from '@/stores/chat'

interface Props {
  message: Message
  onCitationClick: (citation: Citation) => void
  onRetry: () => void
  /** Start vault reindex when the error is index/embedding related. */
  onReindex?: () => void
  indexing?: boolean
}

/** True when chat failed because the vault index needs a rebuild. */
function needsReindex(code: string | undefined): boolean {
  return code === 'index_not_ready' || code === 'embedding_mismatch'
}

/** Render a user or assistant chat turn, including errors, citations, and timing. */
export function MessageBubble({
  message,
  onCitationClick,
  onRetry,
  onReindex,
  indexing,
}: Props) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm leading-relaxed text-primary-foreground">
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>
      </div>
    )
  }

  const empty = !message.content && !message.error && !message.streaming
  const showReindex = message.error && needsReindex(message.error.code) && onReindex

  return (
    <div className="flex flex-col gap-2">
      <div className="max-w-[92%] text-sm">
        {message.content && (
          <div className={cn(message.streaming && 'streaming-caret')}>
            <Markdown text={message.content} />
          </div>
        )}

        {message.streaming && !message.content && (
          <div className="flex gap-1 py-1" aria-label="Thinking">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="size-1.5 animate-bounce rounded-full bg-muted-foreground/50"
                style={{ animationDelay: `${i * 120}ms` }}
              />
            ))}
          </div>
        )}

        {empty && !message.cancelled && (
          <p className="text-sm italic text-muted-foreground">No response.</p>
        )}
      </div>

      {message.error && (
        <div className="flex items-start gap-2.5 rounded-lg border border-destructive/30 bg-destructive/8 px-3 py-2.5">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div className="min-w-0 flex-1">
            <p className="text-sm leading-relaxed text-destructive">{message.error.message}</p>
            <p className="mt-0.5 font-mono text-[11px] text-destructive/70">
              {message.error.code}
            </p>
            {showReindex && (
              <div className="mt-2 flex flex-wrap gap-2">
                <Button
                  variant="default"
                  size="sm"
                  disabled={indexing}
                  onClick={onReindex}
                  className="shrink-0"
                >
                  <Database className="size-3" />
                  {indexing ? 'Reindexing…' : 'Reindex vault'}
                </Button>
                <Button variant="outline" size="sm" onClick={onRetry} className="shrink-0">
                  <RotateCcw className="size-3" />
                  Retry
                </Button>
              </div>
            )}
          </div>
          {!showReindex && (
            <Button variant="outline" size="sm" onClick={onRetry} className="shrink-0">
              <RotateCcw className="size-3" />
              Retry
            </Button>
          )}
        </div>
      )}

      {message.cancelled && (
        <p className="text-xs italic text-muted-foreground">Cancelled.</p>
      )}

      {message.citations.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Sources
          </span>
          {message.citations.map((citation) => (
            <button
              key={citation.id}
              onClick={() => onCitationClick(citation)}
              className="group inline-flex max-w-[220px] items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-xs transition-colors hover:border-primary/50 hover:bg-accent"
              title={citation.note_path}
            >
              <FileText className="size-3 shrink-0 text-muted-foreground group-hover:text-accent-foreground" />
              <span className="truncate">{citation.note_title}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                {citation.score.toFixed(2)}
              </span>
            </button>
          ))}
        </div>
      )}

      {!message.streaming && message.elapsedMs ? (
        <Badge variant="outline" className="w-fit">
          {formatDuration(message.elapsedMs)}
        </Badge>
      ) : null}
    </div>
  )
}
