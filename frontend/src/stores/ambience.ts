import { create } from 'zustand'

/**
 * Ambient / speaking state for the command-center hub.
 *
 * Real browser TTS calls `setSpeaking(true|false)`. WAKE may still use a
 * short filler meter when synthesis is unavailable. STOP cancels speechSynthesis.
 */
interface AmbienceState {
  /** True while Jarvis is "talking" (filler or real TTS). */
  isSpeaking: boolean
  /** 0..1 voice / ambience meter; drives radar pulse intensity. */
  meter: number
  /** Wake / ambient mode armed (listening aesthetics). */
  ambientArmed: boolean
  /** Epoch ms when the current filler session started, or null. */
  fillerStartedAt: number | null
  /** Auto-end timeout handle for filler sessions. */
  _fillerTimer: ReturnType<typeof setTimeout> | null
  /** rAF id for fake meter animation. */
  _meterRaf: number | null

  /** Arm ambient listening UI without starting speech. */
  armAmbient: () => void
  /** Start speaking: filler animation (+ optional durationMs). */
  wake: (durationMs?: number) => void
  /** Stop speaking and cancel filler timers. */
  stop: () => void
  /** Direct speaking flag for real TTS lifecycle hooks. */
  setSpeaking: (speaking: boolean) => void
  /** Push a live amplitude sample (0..1) from real audio analysis. */
  setMeter: (level: number) => void
}

/** Tear down filler interval/raf handles on the store instance. */
function clearFillerInternals(state: AmbienceState) {
  if (state._fillerTimer != null) clearTimeout(state._fillerTimer)
  if (state._meterRaf != null) cancelAnimationFrame(state._meterRaf)
}

/** Drive a time-based meter envelope while speaking (stand-in for AnalyserNode). */
function startMeterLoop(
  get: () => AmbienceState,
  set: (partial: Partial<AmbienceState>) => void,
) {
  const tick = () => {
    const { isSpeaking, fillerStartedAt } = get()
    if (!isSpeaking) {
      set({ meter: 0, _meterRaf: null })
      return
    }
    const t = (Date.now() - (fillerStartedAt ?? Date.now())) / 1000
    // Soft speech-like envelope: slow swell + faster wobble.
    const level =
      0.35 +
      0.35 * Math.sin(t * 2.4) +
      0.2 * Math.sin(t * 7.1 + 0.6) +
      0.1 * Math.sin(t * 13.3)
    set({ meter: Math.max(0.08, Math.min(1, level)), _meterRaf: requestAnimationFrame(tick) })
  }
  set({ _meterRaf: requestAnimationFrame(tick) })
}

export const useAmbienceStore = create<AmbienceState>()((set, get) => ({
  isSpeaking: false,
  meter: 0,
  ambientArmed: false,
  fillerStartedAt: null,
  _fillerTimer: null,
  _meterRaf: null,

  /** Enable ambient listening chrome without speech. */
  armAmbient: () => set({ ambientArmed: true }),

  /**
   * Start ambient arm + speaking chrome.
   * Prefer `speak()` from lib/speech/tts for real voice; this keeps a short
   * meter session when callers only need hub visuals (default 8s, 0 = until STOP).
   */
  wake: (durationMs = 8000) => {
    clearFillerInternals(get())
    set({
      ambientArmed: true,
      isSpeaking: true,
      fillerStartedAt: Date.now(),
      _fillerTimer: null,
      meter: 0.4,
    })
    startMeterLoop(get, set)

    if (durationMs > 0) {
      const timer = setTimeout(() => get().stop(), durationMs)
      set({ _fillerTimer: timer })
    }
  },

  /** Halt speech visuals and cancel browser TTS if active. */
  stop: () => {
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.cancel()
    }
    clearFillerInternals(get())
    set({
      isSpeaking: false,
      meter: 0,
      fillerStartedAt: null,
      _fillerTimer: null,
      _meterRaf: null,
      // Keep ambientArmed so listening chrome stays after STOP.
    })
  },

  /** Hook for real TTS: true on play, false on ended/error. */
  setSpeaking: (speaking) => {
    if (!speaking) {
      get().stop()
      return
    }
    clearFillerInternals(get())
    set({
      ambientArmed: true,
      isSpeaking: true,
      fillerStartedAt: Date.now(),
      _fillerTimer: null,
    })
    startMeterLoop(get, set)
  },

  /** Hook for real TTS audio meter samples. */
  setMeter: (level) => set({ meter: Math.max(0, Math.min(1, level)) }),
}))
