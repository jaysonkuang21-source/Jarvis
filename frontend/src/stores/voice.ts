import { create } from 'zustand'
import { streamVoice } from '@/lib/api/client'
import { getSpeakReplies } from '@/lib/speech/prefs'
import { stripThinkTags } from '@/lib/speech/plainText'
import { beginSpeakStream, stopSpeaking, type SpeakStream } from '@/lib/speech/tts'
import type { Profile, StreamEvent } from '@/lib/api/types'
import { useAppStore } from '@/stores/app'

interface VoiceTurn {
  user: string
  reply: string
  searching: boolean
  error: string | null
}

interface VoiceState {
  open: boolean
  listening: boolean
  streaming: boolean
  turn: VoiceTurn | null
  send: (text: string, profile: Profile) => Promise<void>
  cancel: () => void
  dismiss: () => void
  setListening: (listening: boolean) => void
}

let controller: AbortController | null = null

/** Ephemeral voice agent channel — separate from the RAG chat transcript. */
export const useVoiceStore = create<VoiceState>()((set, get) => ({
  open: false,
  listening: false,
  streaming: false,
  turn: null,

  /** Show/hide listening chrome from the radar hold-to-talk control. */
  setListening: (listening) => set({ listening, open: listening || get().open }),

  /** Close the voice HUD without cancelling an in-flight reply mid-stream. */
  dismiss: () => set({ open: false }),

  /** Abort the in-flight voice stream and stop TTS. */
  cancel: () => {
    const ac = controller
    controller = null
    ac?.abort()
    stopSpeaking()
    set({ streaming: false, listening: false })
  },

  /** Send a spoken (or typed) prompt to the direct voice agent. */
  send: async (text, profile) => {
    const trimmed = text.trim()
    if (!trimmed || get().streaming) return

    stopSpeaking()
    controller?.abort()
    const ac = new AbortController()
    controller = ac

    set({
      open: true,
      listening: false,
      streaming: true,
      turn: { user: trimmed, reply: '', searching: false, error: null },
    })

    let reply = ''
    let speakStream: SpeakStream | null = null
    try {
      await streamVoice({
        message: trimmed,
        history: [],
        profile,
        signal: ac.signal,
        onEvent: (event: StreamEvent) => {
          // Ignore late events from an aborted prior turn.
          if (controller !== ac) return
          switch (event.type) {
            case 'retrieval_start':
            case 'tool_call':
              set((state) => ({
                turn: state.turn
                  ? { ...state.turn, searching: true }
                  : state.turn,
              }))
              break
            case 'tool_result':
              if (
                event.ok &&
                (event.name === 'timer_create' ||
                  event.name === 'timer_cancel' ||
                  event.name === 'timer_list')
              ) {
                void useAppStore.getState().refreshTimers()
              }
              set((state) => ({
                turn: state.turn
                  ? { ...state.turn, searching: false }
                  : state.turn,
              }))
              break
            case 'token':
              reply += event.text
              // Never show leaked model think traces in the voice HUD.
              const visible = stripThinkTags(reply).replace(/\s+/g, ' ').trim()
              set((state) => ({
                turn: state.turn
                  ? { ...state.turn, reply: visible, searching: false }
                  : state.turn,
              }))
              if (getSpeakReplies()) {
                if (!speakStream) speakStream = beginSpeakStream()
                speakStream.push(event.text)
              }
              break
            case 'error':
              stopSpeaking()
              speakStream = null
              set((state) => ({
                turn: state.turn
                  ? { ...state.turn, error: event.message, searching: false }
                  : state.turn,
              }))
              break
            case 'done':
              set((state) => ({
                turn: state.turn
                  ? { ...state.turn, searching: false }
                  : state.turn,
              }))
              speakStream?.end()
              speakStream = null
              break
            default:
              break
          }
        },
      })
    } finally {
      // Only the active turn may clear streaming / controller (cancel→resend race).
      if (controller === ac) {
        controller = null
        set({ streaming: false })
      }
    }
  },
}))