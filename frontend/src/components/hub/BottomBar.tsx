import { useAmbienceStore } from '@/stores/ambience'
import { useAppStore } from '@/stores/app'
import { speak, stopSpeaking } from '@/lib/speech/tts'
import { cn } from '@/lib/utils'

/**
 * Bottom ops bar: ambient label, WAKE / STOP / ASK JARVIS.
 * WAKE speaks a short ready line; STOP cancels TTS.
 */
export function BottomBar() {
  const isSpeaking = useAmbienceStore((s) => s.isSpeaking)
  const meter = useAmbienceStore((s) => s.meter)
  const ambientArmed = useAmbienceStore((s) => s.ambientArmed)
  const stop = useAmbienceStore((s) => s.stop)
  const setChatOpen = useAppStore((s) => s.setChatOpen)
  const setView = useAppStore((s) => s.setView)

  /** Open chat on the hub, or jump to the full chat view if already open. */
  function askJarvis() {
    setView('hub')
    setChatOpen(true)
  }

  /** Speak a brief ready cue so Wake uses real TTS instead of silent filler. */
  function onWake() {
    speak('Ready.')
  }

  return (
    <footer className="panel-ops flex shrink-0 items-center gap-3 border-t border-border px-4 py-2.5">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <div>
          <p className="font-display text-[10px] text-muted-foreground">Ambient Intelligence</p>
          <p className="font-mono text-[9px] tracking-widest text-primary/80">
            {isSpeaking ? 'TRANSMITTING' : ambientArmed ? 'CHANNEL OPEN' : 'DORMANT'}
          </p>
        </div>
        <div
          className="hidden h-6 flex-1 max-w-xs items-end gap-0.5 sm:flex"
          aria-hidden
          title="Voice meter"
        >
          {Array.from({ length: 24 }).map((_, i) => {
            const target = Math.sin(i * 0.55) * 0.5 + 0.5
            const h = isSpeaking ? 20 + meter * target * 80 : ambientArmed ? 18 : 8
            return (
              <span
                key={i}
                className={cn(
                  'w-1 rounded-sm transition-[height] duration-100',
                  isSpeaking ? 'bg-critical/80' : 'bg-primary/35',
                )}
                style={{ height: `${h}%` }}
              />
            )
          })}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onWake}
          className={cn(
            'font-display rounded border px-3 py-1.5 text-[11px] tracking-widest transition-colors',
            isSpeaking
              ? 'border-critical/60 bg-critical/15 text-critical'
              : 'border-critical/50 text-critical hover:bg-critical/10',
          )}
        >
          Wake
        </button>
        <button
          type="button"
          onClick={() => {
            stopSpeaking()
            stop()
          }}
          className="font-display rounded border border-border px-3 py-1.5 text-[11px] tracking-widest text-foreground/90 transition-colors hover:bg-muted"
        >
          Stop
        </button>
        <button
          type="button"
          onClick={askJarvis}
          className="font-display rounded border border-critical/70 bg-critical/20 px-3 py-1.5 text-[11px] tracking-widest text-critical transition-colors hover:bg-critical/30"
        >
          Ask Jarvis
        </button>
      </div>
    </footer>
  )
}
