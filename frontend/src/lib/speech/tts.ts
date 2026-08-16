import { useAmbienceStore } from '@/stores/ambience'
import { api, synthesizeSpeechStream } from '@/lib/api/client'
import { beginPcmTurn, stopPcmPlayback, type PcmTurn } from './pcmPlayer'
import { drainCompletedSpeech, splitForSpeech, stripForSpeech } from './plainText'

/** Bumps on each speak/stop so stale audio callbacks cannot clear a newer turn. */
let speakGeneration = 0

/** How many Fish jobs to keep in flight so the PCM ring stays ahead of playout. */
const PREFETCH_DEPTH = 3

/** Incremental speaker used while an LLM reply is still streaming. */
export interface SpeakStream {
  /** Append one token (or any delta) and speak any newly completed phrases. */
  push: (delta: string) => void
  /** Speak any leftover buffer, then stop when playback drains. */
  end: () => void
}

/** True when Fish PCM playback or Web Speech synthesis is available. */
export function isTtsSupported(): boolean {
  if (typeof window === 'undefined') return false
  return (
    'speechSynthesis' in window ||
    typeof AudioContext !== 'undefined' ||
    typeof (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext !==
      'undefined'
  )
}

/** Cancel any in-flight utterance/audio and clear speaking chrome. */
export function stopSpeaking(): void {
  speakGeneration += 1
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel()
  }
  stopPcmPlayback()
  useAmbienceStore.getState().setSpeaking(false)
}

/**
 * Speak plain text via local Fish Speech into one continuous PCM turn.
 * Web Speech only when Fish is not reachable — never a mid-session timbre flip.
 */
export function speak(text: string): void {
  const clean = stripForSpeech(text)
  if (!clean) return

  const gen = ++speakGeneration
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel()
  }
  stopPcmPlayback()

  useAmbienceStore.getState().setSpeaking(true)
  void playFishChunks(splitForSpeech(clean), gen)
}

/**
 * Start a speak session that voice/chat can feed as tokens arrive.
 * Uses dual chunking (short opening, long follow-ups) and depth-3 Fish
 * prefetch into a continuous PCM ring so phrase boundaries stay gapless.
 */
export function beginSpeakStream(): SpeakStream {
  const gen = ++speakGeneration
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel()
  }
  stopPcmPlayback()
  useAmbienceStore.getState().setSpeaking(true)

  let buffer = ''
  let ended = false
  let started = false
  let failed = false
  let openingDone = false
  const queue: string[] = []
  /** Ordered Fish jobs; up to ``PREFETCH_DEPTH`` may be in flight. */
  const jobs: Array<{ text: string; response: Promise<Response> }> = []
  let pumping = false
  let turnPromise: Promise<PcmTurn> | null = null
  let turn: PcmTurn | null = null

  /** Clear speaking chrome once the turn is over and nothing is left to play. */
  function maybeFinish(): void {
    if (gen !== speakGeneration || failed) return
    if (ended && !queue.length && !jobs.length && !pumping) {
      useAmbienceStore.getState().setSpeaking(false)
    }
  }

  /** Ensure a continuous PCM turn exists for this speak generation. */
  async function ensureTurn(): Promise<PcmTurn | null> {
    if (gen !== speakGeneration || failed) return null
    if (turn) return turn
    if (!turnPromise) {
      turnPromise = beginPcmTurn({ prebufferMs: 220 })
    }
    turn = await turnPromise
    if (gen !== speakGeneration) {
      turn.stop()
      return null
    }
    return turn
  }

  /** Kick Fish requests until the prefetch window is full. */
  function primeJobs(): void {
    if (failed || gen !== speakGeneration) return
    while (jobs.length < PREFETCH_DEPTH && queue.length) {
      const text = queue.shift()!
      jobs.push({ text, response: synthesizeSpeechStream(text) })
    }
  }

  /**
   * Feed queued Fish streams into the continuous ring in order.
   * Does not wait for playout between phrases — only for network PCM.
   */
  async function pump(): Promise<void> {
    if (pumping || gen !== speakGeneration || failed) return
    pumping = true
    try {
      const active = await ensureTurn()
      if (!active || gen !== speakGeneration || failed) return

      while (gen === speakGeneration && !failed) {
        primeJobs()
        if (!jobs.length) {
          if (ended) {
            active.markEnded()
            await active.waitUntilDrained()
            if (gen === speakGeneration && !failed) {
              useAmbienceStore.getState().setSpeaking(false)
            }
          }
          return
        }

        const job = jobs.shift()!
        primeJobs()
        try {
          const response = await job.response
          if (gen !== speakGeneration || failed) return
          started = true
          openingDone = true
          await active.feedStream(response, () => gen === speakGeneration)
          primeJobs()
        } catch (err) {
          failed = true
          await handleFishFailure(err, gen, job.text, started)
          return
        }
      }
    } finally {
      pumping = false
      if (gen === speakGeneration && !failed && (queue.length || jobs.length)) {
        void pump()
      } else {
        maybeFinish()
      }
    }
  }

  /** Enqueue speakable prose and resume the ordered Fish→PCM pipeline. */
  function enqueue(chunks: string[]): void {
    if (gen !== speakGeneration || failed || !chunks.length) return
    queue.push(...chunks)
    primeJobs()
    void pump()
  }

  return {
    push(delta: string) {
      if (gen !== speakGeneration || !delta || failed) return
      buffer += delta
      const { ready, rest } = drainCompletedSpeech(buffer, { openingDone })
      buffer = rest
      if (ready.length) {
        openingDone = true
        enqueue(ready)
      }
    },
    end() {
      if (gen !== speakGeneration || failed) return
      ended = true
      const leftover = stripForSpeech(buffer)
      buffer = ''
      if (leftover) {
        openingDone = true
        enqueue([leftover])
      } else {
        void pump()
        maybeFinish()
      }
    },
  }
}

/**
 * Synthesize a full (already-split) reply into one continuous PCM turn
 * with depth-3 Fish prefetch.
 */
async function playFishChunks(chunks: string[], gen: number): Promise<void> {
  if (!chunks.length) {
    useAmbienceStore.getState().setSpeaking(false)
    return
  }

  let started = false
  let turn: PcmTurn | null = null
  try {
    turn = await beginPcmTurn({ prebufferMs: 220 })
    if (gen !== speakGeneration) {
      turn.stop()
      return
    }

    const pending = [...chunks]
    const jobs: Array<{ text: string; response: Promise<Response> }> = []

    /** Fill the prefetch window from ``pending``. */
    function prime(): void {
      while (jobs.length < PREFETCH_DEPTH && pending.length) {
        const text = pending.shift()!
        jobs.push({ text, response: synthesizeSpeechStream(text) })
      }
    }

    prime()
    while (jobs.length) {
      if (gen !== speakGeneration) return
      const job = jobs.shift()!
      prime()
      const response = await job.response
      if (gen !== speakGeneration) return
      started = true
      await turn.feedStream(response, () => gen === speakGeneration)
      prime()
    }

    turn.markEnded()
    await turn.waitUntilDrained()
    if (gen === speakGeneration) {
      useAmbienceStore.getState().setSpeaking(false)
    }
  } catch (err) {
    if (gen !== speakGeneration) return
    turn?.stop()
    await handleFishFailure(err, gen, chunks.join(' '), started)
  }
}

/**
 * On Fish failure: use Web Speech only when the Fish server is actually down.
 * If Fish is up, stay silent rather than swapping to a browser voice mid-session.
 */
async function handleFishFailure(
  _err: unknown,
  gen: number,
  text: string,
  started: boolean,
): Promise<void> {
  if (gen !== speakGeneration) return
  stopPcmPlayback()
  if (started) {
    useAmbienceStore.getState().setSpeaking(false)
    return
  }
  if (await fishServerReady()) {
    useAmbienceStore.getState().setSpeaking(false)
    return
  }
  speakWebSpeech(text, gen)
}

/** True when Jarvis reports the local Fish Speech server as ready. */
async function fishServerReady(): Promise<boolean> {
  try {
    const st = await api.ttsStatus()
    return Boolean(st.enabled && st.ready)
  } catch {
    return false
  }
}

/** Fallback: OS/browser ``speechSynthesis`` voice (demo / Fish down only). */
function speakWebSpeech(text: string, gen: number): void {
  if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
    useAmbienceStore.getState().setSpeaking(false)
    return
  }
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.rate = 1.05
  utterance.onstart = () => {
    if (gen !== speakGeneration) return
    useAmbienceStore.getState().setSpeaking(true)
  }
  utterance.onend = () => {
    if (gen !== speakGeneration) return
    useAmbienceStore.getState().setSpeaking(false)
  }
  utterance.onerror = () => {
    if (gen !== speakGeneration) return
    useAmbienceStore.getState().setSpeaking(false)
  }
  // Defer so Chromium does not drop the utterance after a same-turn cancel.
  window.setTimeout(() => {
    if (gen !== speakGeneration) return
    window.speechSynthesis.speak(utterance)
  }, 0)
}
