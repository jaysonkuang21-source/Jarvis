import { useEffect, useMemo, useRef } from 'react'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useProfileStore } from '@/stores/profile'
import { cn } from '@/lib/utils'

type SignalLevel = 'WARNING' | 'HEALTHY' | 'CRITICAL' | 'INFO'

interface LiveSignal {
  id: string
  level: SignalLevel
  text: string
}

/** Compose a compact live-signals feed from app state (with tasteful stubs). */
function useLiveSignals(): LiveSignal[] {
  const health = useAppStore((s) => s.health)
  const connected = useAppStore((s) => s.connected)
  const timers = useAppStore((s) => s.timers)
  const metrics = useAppStore((s) => s.metrics)
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const profile = useProfileStore((s) => s.profile)
  const isStreaming = useChatStore((s) => s.isStreaming)
  const retrieval = useChatStore((s) => s.retrieval)

  return useMemo(() => {
    const signals: LiveSignal[] = []

    const backendOk = health?.ok || health?.status === 'healthy'
    signals.push({
      id: 'health',
      level: backendOk ? 'HEALTHY' : 'CRITICAL',
      text: backendOk
        ? `API ${health?.version ?? 'ok'} · ${health?.environment ?? 'local'}`
        : 'Backend unreachable — health check failed',
    })

    signals.push({
      id: 'events',
      level: connected ? 'HEALTHY' : 'WARNING',
      text: connected ? 'EventSource listening for timer fires' : 'Event stream reconnecting',
    })

    if (indexStatus) {
      signals.push({
        id: 'index',
        level: indexStatus.ready
          ? indexStatus.stale_notes > 0
            ? 'WARNING'
            : 'HEALTHY'
          : indexStatus.indexing
            ? 'INFO'
            : 'CRITICAL',
        text: indexStatus.indexing
          ? `Reindex ${indexStatus.indexed_notes}/${indexStatus.total_notes} notes`
          : `Index ${indexStatus.ready ? 'ready' : 'cold'} · ${indexStatus.entities} entities`,
      })
    } else {
      // TODO: replace when /index/status is available without profile load race.
      signals.push({
        id: 'index-stub',
        level: 'INFO',
        text: 'Vault index telemetry pending…',
      })
    }

    if (isStreaming) {
      signals.push({
        id: 'chat',
        level: 'INFO',
        text: retrieval?.label
          ? `Retrieval: ${retrieval.label} (${retrieval.current}/${retrieval.total || '?'})`
          : 'Chat stream active',
      })
    }

    if (timers.length > 0) {
      signals.push({
        id: 'timers',
        level: 'WARNING',
        text: `${timers.length} timer(s) armed`,
      })
    }

    if (metrics) {
      signals.push({
        id: 'metrics',
        level: Number.parseFloat(metrics.error_rate) > 0.05 ? 'WARNING' : 'HEALTHY',
        text: `Latency ${Math.round(metrics.avg_latency_ms)}ms · cache ${metrics.cache_hit_rate}`,
      })
    }

    signals.push({
      id: 'models',
      level: 'INFO',
      text: `Chat ${profile.chat_model} · voice ${profile.voice_model} · embed ${profile.embedding_model}`,
    })

    return signals.slice(0, 7)
  }, [health, connected, timers, metrics, indexStatus, profile, isStreaming, retrieval])
}

/** Level → accent for signal tags. */
function levelClass(level: SignalLevel): string {
  switch (level) {
    case 'CRITICAL':
      return 'text-critical border-critical'
    case 'WARNING':
      return 'text-warning border-warning/50'
    case 'HEALTHY':
      return 'text-success border-success/40'
    default:
      return 'text-info border-border'
  }
}

/** Streaming-style live signals list for the left column. */
export function LiveSignals() {
  const signals = useLiveSignals()
  const hubFocus = useAppStore((s) => s.hubFocus)
  const clearHubFocus = useAppStore((s) => s.clearHubFocus)
  const listRef = useRef<HTMLUListElement>(null)

  /** Scroll/highlight the focused signal when radar HEALTH / CACHE deep-links here. */
  useEffect(() => {
    if (!hubFocus) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-signal-id="${hubFocus}"]`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    const timer = window.setTimeout(() => clearHubFocus(), 2800)
    return () => window.clearTimeout(timer)
  }, [hubFocus, clearHubFocus])

  return (
    <section className="panel-ops flex min-h-0 flex-1 flex-col gap-2 p-3">
      <header className="flex items-center justify-between">
        <h2 className="font-display text-[10px] text-muted-foreground">Live Signals</h2>
        <span className="font-mono text-[9px] text-muted-foreground">{signals.length}</span>
      </header>
      <ul ref={listRef} className="scrollbar-thin flex flex-col gap-1.5 overflow-y-auto">
        {signals.map((signal) => {
          const focused = hubFocus === signal.id
          return (
            <li
              key={signal.id}
              data-signal-id={signal.id}
              className={cn(
                'flex items-start gap-2 border-b border-border/40 pb-1.5 last:border-0 transition-colors',
                focused && 'bg-primary/10 ring-1 ring-primary/40',
              )}
            >
              <span
                className={cn(
                  'mt-0.5 shrink-0 border px-1 py-px font-mono text-[8px] tracking-wider',
                  levelClass(signal.level),
                )}
              >
                {signal.level}
              </span>
              <span className="text-[11px] leading-snug text-foreground/90">{signal.text}</span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
