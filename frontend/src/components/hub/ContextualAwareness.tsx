import { useMemo } from 'react'
import { useAppStore } from '@/stores/app'
import { useProfileStore } from '@/stores/profile'
import { useChatStore } from '@/stores/chat'
import { cn } from '@/lib/utils'

type Severity = 'critical' | 'pressure' | 'healthy'

interface CountBadge {
  severity: Severity
  label: string
  count: number
}

/** Build contextual awareness counts from health, index, and validation state. */
function useAwareness() {
  const health = useAppStore((s) => s.health)
  const connected = useAppStore((s) => s.connected)
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const validation = useProfileStore((s) => s.validation)
  const isStreaming = useChatStore((s) => s.isStreaming)

  return useMemo(() => {
    let critical = 0
    let pressure = 0
    let healthy = 0
    const notes: string[] = []

    const backendOk = health?.ok || health?.status === 'healthy'
    if (!backendOk) {
      critical += 1
      notes.push('Backend health check failing or unreachable.')
    } else {
      healthy += 1
    }

    if (!connected) {
      pressure += 1
      notes.push('Timer event stream disconnected — reconnecting.')
    } else {
      healthy += 1
    }

    if (indexStatus) {
      if (!indexStatus.ready && !indexStatus.indexing) {
        critical += 1
        notes.push('Vault index not ready — open chat error or Settings → Ingestion → Reindex vault.')
      } else if (indexStatus.indexing) {
        pressure += 1
        notes.push('Reindex in progress — answers may omit fresh notes.')
      } else if (indexStatus.stale_notes > 0) {
        pressure += 1
        notes.push(`${indexStatus.stale_notes} stale note(s) pending reindex.`)
        healthy += 1
      } else {
        healthy += 1
      }
    } else {
      pressure += 1
      notes.push('Index status unavailable — showing placeholders.')
    }

    const errors = validation.issues.filter((i) => i.level === 'error')
    const warnings = validation.issues.filter((i) => i.level === 'warning')
    if (errors.length) {
      critical += errors.length
      notes.push(`Profile: ${errors[0]?.message ?? 'blocking validation issues'}`)
    } else {
      healthy += 1
    }
    if (warnings.length) {
      pressure += warnings.length
    }

    if (isStreaming) healthy += 1

    if (notes.length === 0) {
      notes.push('Operating environment nominal. Retrieval and timers stable.')
    }

    const badges: CountBadge[] = [
      { severity: 'critical', label: 'CRITICAL', count: critical },
      { severity: 'pressure', label: 'PRESSURE', count: pressure },
      { severity: 'healthy', label: 'HEALTHY', count: healthy },
    ]

    const fieldTone: Severity =
      critical > 0 ? 'critical' : pressure > 0 ? 'pressure' : 'healthy'

    return { badges, notes, fieldTone }
  }, [health, connected, indexStatus, validation, isStreaming])
}

/** Severity → text color for awareness badges. */
function severityClass(severity: Severity): string {
  switch (severity) {
    case 'critical':
      return 'text-critical'
    case 'pressure':
      return 'text-warning'
    case 'healthy':
      return 'text-success'
  }
}

/** Left-rail contextual awareness summary driven by live telemetry. */
export function ContextualAwareness() {
  const { badges, notes, fieldTone } = useAwareness()

  return (
    <section className="panel-ops space-y-2.5 p-3">
      <h2 className="font-display text-[10px] text-muted-foreground">Contextual Awareness</h2>
      <div className="flex flex-wrap gap-3">
        {badges.map((badge) => (
          <div key={badge.label} className="min-w-[4.5rem]">
            <p className={cn('font-display text-lg leading-none', severityClass(badge.severity))}>
              {badge.count}
            </p>
            <p className={cn('font-mono text-[9px] tracking-wider', severityClass(badge.severity))}>
              {badge.label}
            </p>
          </div>
        ))}
      </div>
      <div>
        <p className="font-display text-[9px] text-muted-foreground">Operating Environment</p>
        <p className="mt-1 text-[11px] leading-relaxed text-foreground/85">{notes[0]}</p>
        {notes[1] && (
          <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">{notes[1]}</p>
        )}
      </div>
      <p
        className={cn(
          'font-mono text-[9px] tracking-widest',
          fieldTone === 'critical'
            ? 'text-critical'
            : fieldTone === 'pressure'
              ? 'text-warning'
              : 'text-success',
        )}
      >
        AWARENESS FIELD:{' '}
        {fieldTone === 'critical'
          ? 'CRITICAL RISK DETECTED'
          : fieldTone === 'pressure'
            ? 'PRESSURE SIGNALS ACTIVE'
            : 'NOMINAL'}
      </p>
    </section>
  )
}
