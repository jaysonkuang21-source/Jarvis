import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Square, Volume2, VolumeX } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { getSpeakReplies, setSpeakReplies } from '@/lib/speech/prefs'
import { isTtsSupported, stopSpeaking } from '@/lib/speech/tts'
import { cn } from '@/lib/utils'

interface Props {
  disabled?: boolean
  streaming: boolean
  placeholder?: string
  onSend: (text: string) => void
  onCancel: () => void
}

/** Chat input with auto-resize and Enter-to-send. Voice lives on the hub radar core. */
export function Composer({ disabled, streaming, placeholder, onSend, onCancel }: Props) {
  const [value, setValue] = useState('')
  const [speakReplies, setSpeakRepliesState] = useState(getSpeakReplies)
  const ref = useRef<HTMLTextAreaElement>(null)
  const ttsOk = isTtsSupported()

  // Grow with the content instead of scrolling a two-line box.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  useEffect(() => {
    if (!streaming) ref.current?.focus()
  }, [streaming])

  /** Send the trimmed draft and clear the textarea. */
  function submit() {
    const trimmed = value.trim()
    if (!trimmed || streaming || disabled) return
    onSend(trimmed)
    setValue('')
  }

  /** Toggle whether Jarvis speaks replies aloud. */
  function toggleSpeakReplies() {
    const next = !speakReplies
    setSpeakReplies(next)
    setSpeakRepliesState(next)
    if (!next) stopSpeaking()
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4">
      <div
        className={cn(
          'flex items-end gap-2 rounded-xl border border-border bg-card p-2 shadow-sm transition-colors',
          'focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-(--color-ring)',
          disabled && 'opacity-60',
        )}
      >
        <textarea
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          placeholder={placeholder ?? 'Ask about your vault…'}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              submit()
            }
          }}
          className="scrollbar-thin max-h-[200px] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
        />

        {ttsOk && (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            title={speakReplies ? 'Mute spoken replies' : 'Speak replies aloud'}
            aria-label={speakReplies ? 'Mute spoken replies' : 'Speak replies aloud'}
            aria-pressed={speakReplies}
            onClick={toggleSpeakReplies}
            className="shrink-0 text-muted-foreground"
          >
            {speakReplies ? <Volume2 className="size-4" /> : <VolumeX className="size-4" />}
          </Button>
        )}

        {streaming ? (
          <Button
            size="icon"
            variant="secondary"
            onClick={() => {
              stopSpeaking()
              onCancel()
            }}
            title="Stop generating"
            aria-label="Stop generating"
          >
            <Square className="size-3.5 fill-current" />
          </Button>
        ) : (
          <Button
            size="icon"
            onClick={submit}
            disabled={!value.trim() || disabled}
            title="Send (Enter)"
            aria-label="Send"
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>

      <p className="mt-1.5 px-1 text-[11px] text-muted-foreground">
        Hold the radar core for the voice agent · Chat is for vault RAG · Enter to send ·
        Shift+Enter for a new line.
      </p>
    </div>
  )
}
