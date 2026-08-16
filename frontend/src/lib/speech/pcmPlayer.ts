/**
 * Turn-scoped continuous PCM playback.
 *
 * Fish phrase streams append into one ring buffer; an AudioWorklet (or a
 * shared-timeline buffer-source fallback) pulls samples continuously so
 * phrase boundaries do not reset the audio clock and create gaps.
 */

/** Shared AudioContext; created lazily after a user gesture when possible. */
let sharedCtx: AudioContext | null = null

/** Active turn player, if any. */
let activeTurn: PcmTurnPlayer | null = null

/** Worklet module URL registered once per page load. */
let workletUrl: string | null = null
let workletReady: Promise<void> | null = null

/** Default audio to buffer before first sample (absorbs Fish/HTTP jitter). */
const DEFAULT_PREBUFFER_MS = 220

/** Return (and resume) an AudioContext, optionally pinned to Fish's rate. */
function getAudioContext(preferredRate?: number): AudioContext {
  if (
    sharedCtx &&
    preferredRate &&
    Math.abs(sharedCtx.sampleRate - preferredRate) > 1
  ) {
    // Recreate when Fish rate disagrees with a prior context (avoids pitch drift).
    void sharedCtx.close().catch(() => undefined)
    sharedCtx = null
    workletReady = null
  }
  if (!sharedCtx) {
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext })
        .webkitAudioContext
    sharedCtx = preferredRate
      ? new Ctx({ sampleRate: preferredRate })
      : new Ctx()
  }
  if (sharedCtx.state === 'suspended') {
    void sharedCtx.resume()
  }
  return sharedCtx
}

/** Inline worklet source: float32 ring buffer drained by the audio thread. */
const WORKLET_SOURCE = `
class JarvisPcmRingProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.capacity = 48000 * 30
    this.buffer = new Float32Array(this.capacity)
    this.readIdx = 0
    this.writeIdx = 0
    this.buffered = 0
    this.prebufferFrames = 0
    this.started = false
    this.ended = false
    this.drainedSent = false
    this.port.onmessage = (ev) => {
      const data = ev.data || {}
      if (data.type === 'config') {
        this.prebufferFrames = data.prebufferFrames | 0
        return
      }
      if (data.type === 'push' && data.samples) {
        const samples = data.samples
        for (let i = 0; i < samples.length; i++) {
          if (this.buffered >= this.capacity) break
          this.buffer[this.writeIdx] = samples[i]
          this.writeIdx++
          if (this.writeIdx >= this.capacity) this.writeIdx = 0
          this.buffered++
        }
        this.port.postMessage({ type: 'stats', buffered: this.buffered })
        return
      }
      if (data.type === 'end') {
        this.ended = true
        return
      }
      if (data.type === 'flush') {
        this.readIdx = 0
        this.writeIdx = 0
        this.buffered = 0
        this.started = false
        this.ended = false
        this.drainedSent = false
      }
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0]
    const n = out.length
    if (!this.started) {
      if (this.buffered >= this.prebufferFrames || (this.ended && this.buffered > 0)) {
        this.started = true
      } else {
        out.fill(0)
        return true
      }
    }
    let i = 0
    while (i < n && this.buffered > 0) {
      out[i++] = this.buffer[this.readIdx]
      this.readIdx++
      if (this.readIdx >= this.capacity) this.readIdx = 0
      this.buffered--
    }
    while (i < n) out[i++] = 0
    if (this.ended && this.buffered === 0 && !this.drainedSent) {
      this.drainedSent = true
      this.port.postMessage({ type: 'drained' })
    }
    return true
  }
}
registerProcessor('jarvis-pcm-ring', JarvisPcmRingProcessor)
`

/** Ensure the ring-buffer worklet module is loaded into ``ctx``. */
async function ensureWorklet(ctx: AudioContext): Promise<boolean> {
  if (typeof ctx.audioWorklet?.addModule !== 'function') return false
  if (!workletUrl) {
    const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' })
    workletUrl = URL.createObjectURL(blob)
  }
  if (!workletReady) {
    workletReady = ctx.audioWorklet.addModule(workletUrl).catch((err) => {
      workletReady = null
      throw err
    })
  }
  try {
    await workletReady
    return true
  } catch {
    return false
  }
}

/** Copy any typed-array view into a fresh ArrayBuffer-backed Uint8Array. */
function copyBytes(src: ArrayBufferView | ArrayBuffer): Uint8Array<ArrayBuffer> {
  const view =
    src instanceof ArrayBuffer
      ? new Uint8Array(src)
      : new Uint8Array(src.buffer, src.byteOffset, src.byteLength)
  const out: Uint8Array<ArrayBuffer> = new Uint8Array(view.byteLength)
  out.set(view)
  return out
}

/** Convert little-endian int16 PCM into Float32 samples in [-1, 1]. */
function pcmS16ToFloat32(pcm: Uint8Array<ArrayBuffer>): Float32Array {
  const samples = pcm.byteLength >> 1
  const out = new Float32Array(samples)
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
  for (let i = 0; i < samples; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768
  }
  return out
}

/** Linear-resample float audio when AudioContext rate ≠ Fish rate. */
function resampleLinear(
  input: Float32Array,
  fromRate: number,
  toRate: number,
): Float32Array {
  if (Math.abs(fromRate - toRate) < 1) return input
  const ratio = fromRate / toRate
  const outLen = Math.max(1, Math.floor(input.length / ratio))
  const out = new Float32Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const src = i * ratio
    const i0 = Math.floor(src)
    const i1 = Math.min(i0 + 1, input.length - 1)
    const frac = src - i0
    out[i] = input[i0]! * (1 - frac) + input[i1]! * frac
  }
  return out
}

/** Turn-scoped continuous PCM sink used by Fish speak sessions. */
export interface PcmTurn {
  /** Append one PCM s16le chunk into the continuous ring (Fish sample rate). */
  enqueuePcm: (pcm: Uint8Array<ArrayBuffer>, fishSampleRate: number) => void
  /** Read a Fish streaming response and append all PCM without waiting for playout. */
  feedStream: (
    response: Response,
    stillActive: () => boolean,
  ) => Promise<void>
  /** Signal that no more PCM will arrive for this turn. */
  markEnded: () => void
  /** Resolve when the ring has drained after ``markEnded``. */
  waitUntilDrained: () => Promise<void>
  /** Hard-stop this turn immediately. */
  stop: () => void
}

/** Worklet-backed continuous player for one speak turn. */
class WorkletPcmTurn implements PcmTurn {
  private node: AudioWorkletNode
  private stopped = false
  private ended = false
  private drained = false
  private drainWaiters: Array<() => void> = []
  private ctxRate: number

  constructor(node: AudioWorkletNode, ctxRate: number, prebufferMs: number) {
    this.node = node
    this.ctxRate = ctxRate
    const prebufferFrames = Math.max(
      1,
      Math.floor((ctxRate * prebufferMs) / 1000),
    )
    this.node.port.postMessage({ type: 'config', prebufferFrames })
    this.node.port.onmessage = (ev: MessageEvent) => {
      if (ev.data?.type === 'drained') {
        this.drained = true
        const waiters = this.drainWaiters
        this.drainWaiters = []
        for (const resolve of waiters) resolve()
      }
    }
    this.node.connect(this.node.context.destination)
  }

  /** Push float samples into the worklet ring. */
  private pushFloat(samples: Float32Array): void {
    if (this.stopped || !samples.length) return
    // Transferable copy so the audio thread owns the buffer.
    const copy = samples.slice()
    this.node.port.postMessage({ type: 'push', samples: copy }, [copy.buffer])
  }

  enqueuePcm(pcm: Uint8Array<ArrayBuffer>, fishSampleRate: number): void {
    if (this.stopped || pcm.byteLength < 2) return
    const even =
      pcm.byteLength % 2 === 0
        ? pcm
        : copyBytes(pcm.subarray(0, pcm.byteLength - 1))
    const floats = resampleLinear(
      pcmS16ToFloat32(even),
      fishSampleRate,
      this.ctxRate,
    )
    this.pushFloat(floats)
  }

  async feedStream(
    response: Response,
    stillActive: () => boolean,
  ): Promise<void> {
    if (!response.body) throw new Error('TTS stream has no body')
    const sampleRate = Number(
      response.headers.get('X-Jarvis-Audio-Sample-Rate') || '44100',
    )
    const fishRate =
      Number.isFinite(sampleRate) && sampleRate > 0 ? sampleRate : 44100
    const reader = response.body.getReader()
    let pending: Uint8Array<ArrayBuffer> = new Uint8Array(0)
    let got = false
    try {
      while (stillActive() && !this.stopped) {
        const { done, value } = await reader.read()
        if (!stillActive() || this.stopped) {
          await reader.cancel().catch(() => undefined)
          break
        }
        if (value?.byteLength) {
          pending = concatPending(pending, copyBytes(value))
          // Push ~20ms frames so the ring stays fed without huge main-thread copies.
          const frameBytes = Math.max(640, Math.floor(fishRate * 0.02) * 2)
          while (pending.byteLength >= frameBytes) {
            const frame = copyBytes(pending.subarray(0, frameBytes))
            pending = copyBytes(pending.subarray(frameBytes))
            this.enqueuePcm(frame, fishRate)
            got = true
          }
        }
        if (done) {
          if (pending.byteLength >= 2) {
            this.enqueuePcm(pending, fishRate)
            got = true
          }
          break
        }
      }
    } finally {
      try {
        reader.releaseLock()
      } catch {
        // already released
      }
    }
    if (!got && stillActive() && !this.stopped) {
      throw new Error('TTS stream produced no audio')
    }
  }

  markEnded(): void {
    if (this.stopped || this.ended) return
    this.ended = true
    this.node.port.postMessage({ type: 'end' })
  }

  waitUntilDrained(): Promise<void> {
    if (this.stopped || this.drained) return Promise.resolve()
    return new Promise((resolve) => {
      if (this.drained) {
        resolve()
        return
      }
      this.drainWaiters.push(resolve)
      // Safety timeout so a silent underrun cannot hang speaking chrome.
      window.setTimeout(resolve, 120_000)
    })
  }

  stop(): void {
    if (this.stopped) return
    this.stopped = true
    this.node.port.postMessage({ type: 'flush' })
    try {
      this.node.disconnect()
    } catch {
      // already disconnected
    }
    const waiters = this.drainWaiters
    this.drainWaiters = []
    for (const resolve of waiters) resolve()
  }
}

/** Fallback when AudioWorklet is unavailable: shared-timeline buffer sources. */
class TimelinePcmTurn implements PcmTurn {
  private ctx: AudioContext
  private nextTime: number
  private sources: AudioBufferSourceNode[] = []
  private stopped = false
  private ended = false
  private queuedThrough = 0
  private drainWaiters: Array<() => void> = []
  private prebufferSec: number
  private started = false

  constructor(ctx: AudioContext, prebufferMs: number) {
    this.ctx = ctx
    this.prebufferSec = Math.max(0.05, prebufferMs / 1000)
    this.nextTime = ctx.currentTime + 0.02
  }

  enqueuePcm(pcm: Uint8Array<ArrayBuffer>, fishSampleRate: number): void {
    if (this.stopped || pcm.byteLength < 2) return
    const even =
      pcm.byteLength % 2 === 0
        ? pcm
        : copyBytes(pcm.subarray(0, pcm.byteLength - 1))
    const floats = resampleLinear(
      pcmS16ToFloat32(even),
      fishSampleRate,
      this.ctx.sampleRate,
    )
    if (!floats.length) return
    const buffer = this.ctx.createBuffer(1, floats.length, this.ctx.sampleRate)
    buffer.getChannelData(0).set(floats)
    const source = this.ctx.createBufferSource()
    source.buffer = buffer
    source.connect(this.ctx.destination)
    if (!this.started) {
      this.nextTime = Math.max(
        this.ctx.currentTime + this.prebufferSec,
        this.nextTime,
      )
      this.started = true
    }
    const startAt = Math.max(this.ctx.currentTime + 0.005, this.nextTime)
    source.start(startAt)
    this.nextTime = startAt + buffer.duration
    this.queuedThrough = this.nextTime
    this.sources.push(source)
    source.onended = () => {
      this.sources = this.sources.filter((s) => s !== source)
      this.maybeResolveDrain()
    }
  }

  async feedStream(
    response: Response,
    stillActive: () => boolean,
  ): Promise<void> {
    if (!response.body) throw new Error('TTS stream has no body')
    const sampleRate = Number(
      response.headers.get('X-Jarvis-Audio-Sample-Rate') || '44100',
    )
    const fishRate =
      Number.isFinite(sampleRate) && sampleRate > 0 ? sampleRate : 44100
    const reader = response.body.getReader()
    let pending: Uint8Array<ArrayBuffer> = new Uint8Array(0)
    let got = false
    try {
      while (stillActive() && !this.stopped) {
        const { done, value } = await reader.read()
        if (!stillActive() || this.stopped) {
          await reader.cancel().catch(() => undefined)
          break
        }
        if (value?.byteLength) {
          pending = concatPending(pending, copyBytes(value))
          const frameBytes = Math.max(640, Math.floor(fishRate * 0.02) * 2)
          while (pending.byteLength >= frameBytes) {
            const frame = copyBytes(pending.subarray(0, frameBytes))
            pending = copyBytes(pending.subarray(frameBytes))
            this.enqueuePcm(frame, fishRate)
            got = true
          }
        }
        if (done) {
          if (pending.byteLength >= 2) {
            this.enqueuePcm(pending, fishRate)
            got = true
          }
          break
        }
      }
    } finally {
      try {
        reader.releaseLock()
      } catch {
        // already released
      }
    }
    if (!got && stillActive() && !this.stopped) {
      throw new Error('TTS stream produced no audio')
    }
  }

  markEnded(): void {
    this.ended = true
    this.maybeResolveDrain()
  }

  waitUntilDrained(): Promise<void> {
    if (this.stopped) return Promise.resolve()
    return new Promise((resolve) => {
      this.drainWaiters.push(resolve)
      const remainingMs = Math.max(
        0,
        (this.queuedThrough - this.ctx.currentTime) * 1000,
      )
      window.setTimeout(() => resolve(), remainingMs + 80)
      this.maybeResolveDrain()
    })
  }

  /** Resolve drain waiters once ended and nothing is left scheduled. */
  private maybeResolveDrain(): void {
    if (!this.ended || this.sources.length) return
    const waiters = this.drainWaiters
    this.drainWaiters = []
    for (const resolve of waiters) resolve()
  }

  stop(): void {
    if (this.stopped) return
    this.stopped = true
    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        // already stopped
      }
      try {
        source.disconnect()
      } catch {
        // already disconnected
      }
    }
    this.sources = []
    const waiters = this.drainWaiters
    this.drainWaiters = []
    for (const resolve of waiters) resolve()
  }
}

type PcmTurnPlayer = WorkletPcmTurn | TimelinePcmTurn

/** Append ``extra`` onto ``base`` without sharing buffers. */
function concatPending(
  base: Uint8Array<ArrayBuffer>,
  extra: Uint8Array<ArrayBuffer>,
): Uint8Array<ArrayBuffer> {
  if (!base.byteLength) return extra
  if (!extra.byteLength) return base
  const out: Uint8Array<ArrayBuffer> = new Uint8Array(
    base.byteLength + extra.byteLength,
  )
  out.set(base, 0)
  out.set(extra, base.byteLength)
  return out
}

/** Hard-stop any active turn and clear speaking audio. */
export function stopPcmPlayback(): void {
  if (activeTurn) {
    activeTurn.stop()
    activeTurn = null
  }
}

/**
 * Start a continuous PCM turn for one assistant reply.
 * Prefer AudioWorklet ring buffer; fall back to shared-timeline buffer sources.
 */
export async function beginPcmTurn(options?: {
  sampleRate?: number
  prebufferMs?: number
}): Promise<PcmTurn> {
  stopPcmPlayback()
  const fishRate = options?.sampleRate ?? 44100
  const prebufferMs = options?.prebufferMs ?? DEFAULT_PREBUFFER_MS
  const ctx = getAudioContext(fishRate)
  const useWorklet = await ensureWorklet(ctx)
  let turn: PcmTurnPlayer
  if (useWorklet) {
    const node = new AudioWorkletNode(ctx, 'jarvis-pcm-ring', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
    turn = new WorkletPcmTurn(node, ctx.sampleRate, prebufferMs)
  } else {
    turn = new TimelinePcmTurn(ctx, prebufferMs)
  }
  activeTurn = turn
  return turn
}
