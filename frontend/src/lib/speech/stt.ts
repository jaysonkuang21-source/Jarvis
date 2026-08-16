import { useAmbienceStore } from '@/stores/ambience'

/** Minimal SpeechRecognition surface used by push-to-talk. */
type BrowserSpeechRecognition = {
  continuous: boolean
  interimResults: boolean
  lang: string
  start: () => void
  stop: () => void
  abort: () => void
  onresult: ((event: SpeechRecognitionResultEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
}

type SpeechRecognitionResultEvent = {
  resultIndex: number
  results: ArrayLike<{
    isFinal: boolean
    0: { transcript: string }
  }>
}

type RecognitionCtor = new () => BrowserSpeechRecognition

/** Resolve the Chromium `webkit` constructor or the standard one. */
function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor
    webkitSpeechRecognition?: RecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

/** True when browser speech recognition is available (typically Chromium/Edge). */
export function isSttSupported(): boolean {
  return recognitionCtor() != null
}

export interface PushToTalkHandlers {
  onInterim: (text: string) => void
  /** Called once when the hold session ends; empty string means no speech. */
  onFinal: (text: string) => void
  onError: (message: string) => void
}

/**
 * Create a hold-to-talk recognizer. Call `start` on pointer down and `stop` on up.
 * Arms ambient listening chrome while the mic is open.
 *
 * Session ids ignore stale `onend` from an aborted prior instance so a rapid
 * re-press cannot orphan the live recognition (mic leak) or double-send.
 */
export function createPushToTalk(handlers: PushToTalkHandlers): {
  start: () => void
  stop: () => void
} | null {
  const Ctor = recognitionCtor()
  if (!Ctor) return null

  let recognition: BrowserSpeechRecognition | null = null
  let finals = ''
  let lastHeard = ''
  /** Bumped on each start/force-abort so stale callbacks become no-ops. */
  let sessionId = 0

  /** Drop handlers and abort so a dying instance cannot touch the next session. */
  function discardRecognition(rec: BrowserSpeechRecognition | null) {
    if (!rec) return
    rec.onresult = null
    rec.onerror = null
    rec.onend = null
    try {
      rec.abort()
    } catch {
      /* ignore */
    }
  }

  return {
    /** Begin listening; previous session is aborted first. */
    start: () => {
      sessionId += 1
      const mySession = sessionId
      discardRecognition(recognition)
      recognition = null
      finals = ''
      lastHeard = ''

      const rec = new Ctor()
      recognition = rec
      rec.continuous = true
      rec.interimResults = true
      rec.lang = navigator.language || 'en-US'

      rec.onresult = (event) => {
        if (mySession !== sessionId || recognition !== rec) return
        let interim = ''
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const piece = event.results[i][0]?.transcript ?? ''
          if (event.results[i].isFinal) finals = `${finals} ${piece}`.trim()
          else interim += piece
        }
        const display = `${finals} ${interim}`.trim()
        if (display) {
          lastHeard = display
          handlers.onInterim(display)
        }
      }

      rec.onerror = (event) => {
        if (mySession !== sessionId || recognition !== rec) return
        if (event.error === 'aborted' || event.error === 'no-speech') return
        handlers.onError(
          event.error === 'not-allowed' ? 'Microphone permission denied' : event.error,
        )
      }

      rec.onend = () => {
        if (mySession !== sessionId || recognition !== rec) return
        recognition = null
        const text = (finals || lastHeard).trim()
        handlers.onFinal(text)
      }

      try {
        useAmbienceStore.getState().armAmbient()
        rec.start()
      } catch {
        if (mySession === sessionId) {
          recognition = null
          handlers.onError('Could not start microphone')
        }
      }
    },

    /** End listening; `onFinal` runs from `onend` (empty string if silence). */
    stop: () => {
      const rec = recognition
      if (!rec) return
      try {
        rec.stop()
      } catch {
        if (recognition === rec) {
          sessionId += 1
          recognition = null
          handlers.onFinal((finals || lastHeard).trim())
        }
      }
    },
  }
}
