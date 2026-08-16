import { Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useVoiceStore } from '@/stores/voice'
import { cn } from '@/lib/utils'

/** Compact hub overlay for voice agent turns (not the RAG chat transcript). */
export function VoiceHud() {
  const open = useVoiceStore((s) => s.open)
  const listening = useVoiceStore((s) => s.listening)
  const streaming = useVoiceStore((s) => s.streaming)
  const turn = useVoiceStore((s) => s.turn)
  const dismiss = useVoiceStore((s) => s.dismiss)
  const cancel = useVoiceStore((s) => s.cancel)

  if (!open && !listening) return null

  return (
    <div
      className={cn(
        'panel-ops pointer-events-auto absolute bottom-16 left-1/2 z-30 w-[min(420px,92vw)] -translate-x-1/2',
        'rounded-lg border border-border/80 px-3 py-2.5 shadow-lg',
      )}
      role="status"
      aria-live="polite"
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <p className="font-display text-[10px] tracking-widest text-primary/90">
          {listening ? 'LISTENING' : streaming ? 'VOICE AGENT' : 'VOICE'}
          {turn?.searching ? ' · SEARCHING VAULT' : ''}
        </p>
        <div className="flex items-center gap-1">
          {streaming && (
            <Button type="button" size="sm" variant="ghost" onClick={cancel} className="h-6 px-2 text-[10px]">
              Stop
            </Button>
          )}
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-6"
            aria-label="Dismiss voice panel"
            onClick={dismiss}
          >
            <X className="size-3.5" />
          </Button>
        </div>
      </div>

      {turn?.user && (
        <p className="mb-1 text-[11px] text-muted-foreground">
          You: <span className="text-foreground/90">{turn.user}</span>
        </p>
      )}

      {(streaming || turn?.reply) && (
        <p className="text-sm leading-snug text-foreground">
          {turn?.searching && !turn.reply ? (
            <span className="inline-flex items-center gap-1.5 text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              Checking vault…
            </span>
          ) : (
            turn?.reply || (streaming ? '…' : '')
          )}
        </p>
      )}

      {turn?.error && (
        <p className="mt-1 text-xs text-destructive" role="alert">
          {turn.error}
        </p>
      )}

      {!turn && listening && (
        <p className="text-xs text-muted-foreground">Release to send to the voice agent.</p>
      )}
    </div>
  )
}
