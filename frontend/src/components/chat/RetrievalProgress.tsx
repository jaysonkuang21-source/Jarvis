import { useEffect, useState } from 'react'
import { Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { RetrievalState } from '@/stores/chat'

interface Props {
  retrieval: RetrievalState
  onCancel: () => void
}

/**
 * Broad search runs one LLM call per community report, which on a local model
 * is minutes. Without a visible count and a way out, that is indistinguishable
 * from a hang, so this row is load-bearing rather than decoration.
 */
export function RetrievalProgress({ retrieval, onCancel }: Props) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const started = retrieval.startedAt || Date.now()
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 250)
    return () => clearInterval(id)
  }, [retrieval.startedAt])

  const { current, total, estimatedSeconds } = retrieval
  const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : null
  const overdue = estimatedSeconds !== null && elapsed > estimatedSeconds * 1.5

  return (
    <div className="mx-auto w-full max-w-3xl px-4">
      <div className="rounded-lg border border-border bg-muted/50 px-3.5 py-3">
        <div className="flex items-center gap-2.5">
          <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
          <span className="min-w-0 flex-1 truncate text-sm">{retrieval.label}</span>

          {total > 0 && (
            <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
              {current}/{total}
            </span>
          )}

          <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
            {elapsed}s
            {estimatedSeconds !== null && (
              <span className="opacity-60"> / ~{Math.round(estimatedSeconds)}s</span>
            )}
          </span>

          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onCancel}
            title="Cancel this run"
            aria-label="Cancel this run"
          >
            <X className="size-3.5" />
          </Button>
        </div>

        <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-input">
          <div
            className={cn(
              'h-full rounded-full bg-primary transition-[width] duration-200',
              pct === null && 'w-1/3 animate-pulse',
            )}
            style={pct === null ? undefined : { width: `${pct}%` }}
          />
        </div>

        {overdue && (
          <p className="mt-2 text-xs text-muted-foreground">
            Taking longer than estimated. Local models are slow at this; cancel any time.
          </p>
        )}
      </div>
    </div>
  )
}
