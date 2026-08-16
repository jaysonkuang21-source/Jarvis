import { useEffect, useRef, useState } from 'react'
import { useAmbienceStore } from '@/stores/ambience'
import { useAppStore, type SettingsTab, type View } from '@/stores/app'
import { useProfileStore } from '@/stores/profile'
import { useVoiceStore } from '@/stores/voice'
import { createPushToTalk, isSttSupported } from '@/lib/speech/stt'
import { stopSpeaking } from '@/lib/speech/tts'
import { cn } from '@/lib/utils'

type RingTarget =
  | { kind: 'view'; view: View }
  | { kind: 'settings'; tab: SettingsTab; focus?: string }
  | { kind: 'chat' }
  | { kind: 'hub'; panel: string }

const RING_NODES: ReadonlyArray<{
  label: string
  angle: number
  target: RingTarget
}> = [
  { label: 'RETRIEVAL', angle: -90, target: { kind: 'settings', tab: 'retrieval', focus: 'retrieval-query' } },
  { label: 'INGEST', angle: -40, target: { kind: 'settings', tab: 'ingestion', focus: 'ingestion-pipeline' } },
  { label: 'POLICY', angle: 10, target: { kind: 'settings', tab: 'rules' } },
  { label: 'TIMERS', angle: 55, target: { kind: 'view', view: 'timers' } },
  { label: 'GRAPH', angle: 110, target: { kind: 'settings', tab: 'ingestion', focus: 'ingestion-index' } },
  { label: 'CHAT', angle: 155, target: { kind: 'chat' } },
  { label: 'VAULT', angle: 200, target: { kind: 'settings', tab: 'rules', focus: 'rules-vault' } },
  { label: 'INDEX', angle: 250, target: { kind: 'settings', tab: 'ingestion', focus: 'ingestion-index' } },
  { label: 'CACHE', angle: 295, target: { kind: 'hub', panel: 'metrics' } },
  { label: 'HEALTH', angle: 340, target: { kind: 'hub', panel: 'health' } },
]

/** Polar → cartesian helper for ring node labels. */
function polar(cx: number, cy: number, r: number, deg: number) {
  const rad = ((deg - 90) * Math.PI) / 180
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
}

/** Whether the user prefers reduced motion (freeze continuous pulse intensity). */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof window !== 'undefined'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false,
  )
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    /** Sync local state when the OS reduced-motion preference changes. */
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

/**
 * Central radar / sonar hub. Hold the core to talk; release sends to the agent.
 * Ring spin / scan run from load (STANDBY) via always-on `.hub-armed`.
 */
export function RadarHub() {
  const isSpeaking = useAmbienceStore((s) => s.isSpeaking)
  const meter = useAmbienceStore((s) => s.meter)
  const ambientArmed = useAmbienceStore((s) => s.ambientArmed)
  const setView = useAppStore((s) => s.setView)
  const setChatOpen = useAppStore((s) => s.setChatOpen)
  const openSettings = useAppStore((s) => s.openSettings)
  const focusHubSignal = useAppStore((s) => s.focusHubSignal)
  const pushToast = useAppStore((s) => s.pushToast)
  const reduceMotion = usePrefersReducedMotion()

  const [listening, setListening] = useState(false)
  const [micError, setMicError] = useState<string | null>(null)
  const pttRef = useRef<ReturnType<typeof createPushToTalk>>(null)
  const listeningRef = useRef(false)
  const acceptFinalRef = useRef(true)
  const sttOk = isSttSupported()

  useEffect(() => {
    pttRef.current = createPushToTalk({
      onInterim: () => {
        /* Interim text is optional on the hub; LISTENING chrome is enough. */
      },
      onFinal: (text) => {
        setListening(false)
        listeningRef.current = false
        useVoiceStore.getState().setListening(false)
        if (!acceptFinalRef.current) return
        const trimmed = text.trim()
        if (!trimmed) return
        const profile = useProfileStore.getState().profile
        const voice = useVoiceStore.getState()
        if (voice.streaming) {
          pushToast('VOICE', 'Wait for the current voice reply to finish')
          return
        }
        setView('hub')
        void voice.send(trimmed, profile)
      },
      onError: (message) => {
        setMicError(message)
        setListening(false)
        listeningRef.current = false
        useVoiceStore.getState().setListening(false)
        pushToast('VOICE', message)
      },
    })
    return () => {
      acceptFinalRef.current = false
      pttRef.current?.stop()
      pttRef.current = null
    }
  }, [pushToast, setView])

  const liveMeter = reduceMotion ? 0 : meter
  const pulse = reduceMotion
    ? 0.35
    : isSpeaking
      ? 0.7 + liveMeter * 0.55
      : listening || ambientArmed
        ? 0.35
        : 0.2
  const coreScale = reduceMotion ? 1 : isSpeaking || listening ? 1 + liveMeter * 0.12 : 1

  /** Navigate from a hub ring label to the matching surface with status context. */
  function go(target: RingTarget, label: string) {
    if (target.kind === 'chat') {
      setView('hub')
      setChatOpen(true)
      pushToast('CHAT', 'Ask Jarvis drawer opened')
      return
    }
    if (target.kind === 'settings') {
      openSettings(target.tab, target.focus)
      return
    }
    if (target.kind === 'hub') {
      const { health, metrics, connected } = useAppStore.getState()
      const indexStatus = useProfileStore.getState().indexStatus
      if (target.panel === 'health') {
        const ok = health?.ok || health?.status === 'healthy'
        focusHubSignal('health', {
          title: 'HEALTH',
          body: ok
            ? `API ${health?.version ?? 'ok'} · ${health?.environment ?? 'local'} · events ${connected ? 'live' : 'reconnecting'}`
            : 'Backend unreachable — check the API process',
        })
        return
      }
      if (target.panel === 'metrics') {
        focusHubSignal('metrics', {
          title: 'CACHE / METRICS',
          body: metrics
            ? `Cache ${metrics.cache_hit_rate} · latency ${Math.round(metrics.avg_latency_ms)}ms · errors ${metrics.error_rate}`
            : 'Metrics pending — telemetry refreshes every 15s',
        })
        return
      }
      focusHubSignal(target.panel, {
        title: label,
        body: indexStatus
          ? `Index ${indexStatus.ready ? 'ready' : 'cold'} · ${indexStatus.entities} entities`
          : 'Hub signal',
      })
      return
    }
    if (target.view === 'timers') {
      const count = useAppStore.getState().timers.length
      pushToast('TIMERS', count > 0 ? `${count} pending job(s)` : 'No timers armed')
    }
    setView(target.view)
  }

  /** Start hold-to-talk on the radar core. */
  function beginTalk() {
    if (!sttOk || listeningRef.current || useVoiceStore.getState().streaming) return
    stopSpeaking()
    setMicError(null)
    acceptFinalRef.current = true
    listeningRef.current = true
    setListening(true)
    useVoiceStore.getState().setListening(true)
    pttRef.current?.start()
  }

  /** Release hold-to-talk; final transcript is sent via onFinal. */
  function endTalk() {
    if (!listeningRef.current) return
    pttRef.current?.stop()
  }

  const statusLabel = isSpeaking
    ? 'SPEAKING'
    : listening
      ? 'LISTENING'
      : ambientArmed
        ? 'ARMED'
        : 'STANDBY'

  return (
    <div
      className={cn(
        'relative flex aspect-square w-full max-w-[min(56vh,520px)] items-center justify-center',
        'hub-armed',
        (isSpeaking || listening) && 'hub-speaking',
      )}
      aria-label={isSpeaking ? 'Jarvis speaking' : 'Jarvis operational awareness hub'}
    >
      <svg
        viewBox="0 0 400 400"
        className="h-full w-full overflow-visible"
        role="group"
        aria-label="Operational awareness radar"
      >
        <defs>
          <radialGradient id="hub-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="oklch(0.55 0.22 25)" stopOpacity={0.95} />
            <stop offset="55%" stopColor="oklch(0.4 0.18 25)" stopOpacity={0.7} />
            <stop offset="100%" stopColor="oklch(0.2 0.08 25)" stopOpacity={0} />
          </radialGradient>
          <radialGradient id="hub-teal" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="oklch(0.74 0.14 185)" stopOpacity={0.25 * pulse} />
            <stop offset="100%" stopColor="oklch(0.74 0.14 185)" stopOpacity={0} />
          </radialGradient>
        </defs>

        <circle cx="200" cy="200" r="190" fill="url(#hub-teal)" className="hub-core-glow" />

        {[60, 95, 130, 165, 190].map((r, i) => (
          <circle
            key={r}
            cx="200"
            cy="200"
            r={r}
            fill="none"
            stroke={i % 2 === 0 ? 'oklch(0.72 0.12 185 / 0.45)' : 'oklch(0.7 0.05 210 / 0.28)'}
            strokeWidth={i === 4 ? 1.2 : 0.8}
            strokeDasharray={i === 1 || i === 3 ? '2 6' : undefined}
            className={cn(
              i % 2 === 0 ? 'hub-ring-spin' : 'hub-ring-spin-rev',
              'hub-ring-breathe origin-center',
            )}
            style={{ transformOrigin: '200px 200px', opacity: 0.35 + pulse * 0.4 }}
          />
        ))}

        <g className="hub-scan origin-center" style={{ transformOrigin: '200px 200px' }}>
          <path
            d="M200 200 L200 20 A180 180 0 0 1 320 55 Z"
            fill="oklch(0.72 0.14 185 / 0.08)"
            stroke="oklch(0.72 0.14 185 / 0.25)"
            strokeWidth="0.5"
          />
        </g>

        {[70, 110, 150].map((r, i) => {
          const p = polar(200, 200, r, i * 80 + 20)
          return (
            <circle
              key={r}
              cx={p.x}
              cy={p.y}
              r={isSpeaking && !reduceMotion ? 2.5 + liveMeter * 2 : 2}
              fill="oklch(0.78 0.12 185)"
              className="hub-ring-spin"
              style={{ transformOrigin: '200px 200px', opacity: 0.5 + pulse * 0.5 }}
            />
          )
        })}

        {RING_NODES.map((node) => {
          const p = polar(200, 200, 178, node.angle)
          return (
            <g key={node.label}>
              <circle
                cx={p.x}
                cy={p.y}
                r={14}
                fill="transparent"
                className="cursor-pointer focus:outline-none focus:fill-primary/15"
                role="link"
                tabIndex={0}
                aria-label={`Open ${node.label}`}
                onClick={() => go(node.target, node.label)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    go(node.target, node.label)
                  }
                }}
              />
              <text
                x={p.x}
                y={p.y}
                textAnchor="middle"
                dominantBaseline="middle"
                pointerEvents="none"
                className="fill-muted-foreground"
                style={{ fontSize: 7, letterSpacing: '0.12em', fontFamily: 'inherit' }}
              >
                {node.label}
              </text>
            </g>
          )
        })}

        <circle
          cx="200"
          cy="200"
          r={48 * coreScale}
          fill="url(#hub-core)"
          className="hub-core-glow"
          style={{ pointerEvents: 'none' }}
        />
        <circle
          cx="200"
          cy="200"
          r={36 * coreScale}
          fill="none"
          stroke={
            listening
              ? 'oklch(0.72 0.14 185 / 0.95)'
              : 'oklch(0.62 0.22 25 / 0.85)'
          }
          strokeWidth="1.2"
          className="hub-core-glow"
          style={{ pointerEvents: 'none' }}
        />
      </svg>

      <button
        type="button"
        disabled={!sttOk}
        aria-label={sttOk ? 'Hold to talk to Jarvis' : 'Voice requires Chromium or Edge'}
        aria-pressed={listening}
        title={
          sttOk
            ? 'Hold to talk — release to send'
            : 'Voice needs Chromium/Edge with microphone access'
        }
        onPointerDown={(event) => {
          if (!sttOk) return
          event.preventDefault()
          ;(event.currentTarget as HTMLButtonElement).setPointerCapture(event.pointerId)
          beginTalk()
        }}
        onPointerUp={endTalk}
        onPointerCancel={endTalk}
        onLostPointerCapture={endTalk}
        className={cn(
          'absolute z-10 flex size-[7.25rem] cursor-pointer flex-col items-center justify-center rounded-full',
          'bg-transparent text-center outline-none select-none',
          'focus-visible:ring-2 focus-visible:ring-critical/60',
          listening && 'ring-2 ring-critical/50',
          !sttOk && 'cursor-not-allowed opacity-80',
        )}
      >
        <span
          className="font-display text-[10px] font-semibold leading-tight text-critical"
          style={{ textShadow: '0 0 18px var(--glow-red)' }}
        >
          JARVIS
          <br />
          OPERATIONAL
          <br />
          AWARENESS
        </span>
        <span className="mt-2 font-mono text-[9px] tracking-widest text-primary/80">
          {statusLabel}
        </span>
      </button>

      <p className="pointer-events-none absolute -bottom-6 left-1/2 w-max max-w-[90%] -translate-x-1/2 text-center font-mono text-[9px] tracking-wider text-muted-foreground">
        {listening
          ? 'RELEASE TO SEND'
          : micError
            ? micError.toUpperCase()
            : sttOk
              ? 'HOLD CORE TO SPEAK · VOICE AGENT'
              : 'VOICE UNAVAILABLE'}
      </p>
    </div>
  )
}
