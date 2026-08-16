import { useMemo } from 'react'
import { useAppStore } from '@/stores/app'
import { useProfileStore } from '@/stores/profile'
import { useAmbienceStore } from '@/stores/ambience'
import { cn } from '@/lib/utils'

interface Pursuit {
  id: string
  title: string
  progress: number
  status: 'ON TRACK' | 'AHEAD' | 'AT RISK' | 'IDLE'
  detail: string
}

/** Map Jarvis readiness metrics into pursuit-style goal rows. */
function usePursuits(): { pursuits: Pursuit[]; trajectory: string; tone: 'critical' | 'ok' } {
  const indexStatus = useProfileStore((s) => s.indexStatus)
  const validation = useProfileStore((s) => s.validation)
  const profile = useProfileStore((s) => s.profile)
  const timers = useAppStore((s) => s.timers)
  const connected = useAppStore((s) => s.connected)
  const health = useAppStore((s) => s.health)
  const ambientArmed = useAmbienceStore((s) => s.ambientArmed)

  return useMemo(() => {
    const indexProgress = indexStatus
      ? indexStatus.total_notes === 0
        ? 0
        : Math.min(
            100,
            Math.round(
              ((indexStatus.indexed_notes || indexStatus.total_notes) /
                Math.max(1, indexStatus.total_notes)) *
                100,
            ),
          )
      : 0

    const profileReady = validation.valid ? 100 : 45
    const vaultSync = indexStatus?.stale_notes
      ? Math.max(20, 100 - indexStatus.stale_notes * 5)
      : indexStatus?.ready
        ? 92
        : 30
    const policyGuard = validation.issues.some((i) => i.level === 'error') ? 40 : 88
    const timerCoverage = connected ? (timers.length > 0 ? 75 : 60) : 35
    const ambient = ambientArmed ? 80 : 25

    const pursuits: Pursuit[] = [
      {
        id: 'index',
        title: 'Index Health',
        progress: indexProgress || (indexStatus?.ready ? 86 : 18),
        status: indexStatus?.ready
          ? indexStatus.stale_notes > 0
            ? 'AT RISK'
            : 'ON TRACK'
          : indexStatus?.indexing
            ? 'AHEAD'
            : 'AT RISK',
        detail: indexStatus
          ? `${indexStatus.indexed_notes}/${indexStatus.total_notes} notes`
          : 'No index telemetry',
      },
      {
        id: 'vault',
        title: 'Vault Sync',
        progress: vaultSync,
        status: vaultSync >= 80 ? 'ON TRACK' : vaultSync >= 50 ? 'IDLE' : 'AT RISK',
        detail: indexStatus
          ? `${indexStatus.stale_notes} stale · engine ${indexStatus.engine}`
          : 'Awaiting /index/status',
      },
      {
        id: 'policy',
        title: 'Policy Guard',
        progress: policyGuard,
        status: policyGuard >= 80 ? 'ON TRACK' : 'AT RISK',
        detail: validation.valid ? 'Profile valid' : 'Fix settings blockers',
      },
      {
        id: 'profile',
        title: 'Profile Readiness',
        progress: profileReady,
        status: validation.valid ? 'AHEAD' : 'AT RISK',
        detail: `${profile.rag_mode} · ${profile.query_mode}`,
      },
      {
        id: 'timers',
        title: 'Timer Lattice',
        progress: timerCoverage,
        status: connected ? (timers.length ? 'ON TRACK' : 'IDLE') : 'AT RISK',
        detail: health?.ok ? `${timers.length} pending` : 'Backend check soft',
      },
      {
        id: 'ambient',
        title: 'Ambient Channel',
        progress: ambient,
        status: ambientArmed ? 'ON TRACK' : 'IDLE',
        detail: ambientArmed
          ? 'Fish Speech S1-mini · stream speak'
          : 'TTS stand-by · WAKE arms spoken replies',
      },
    ]

    const atRisk = pursuits.filter((p) => p.status === 'AT RISK').length
    const trajectory =
      atRisk >= 2
        ? 'REPRIORITIZING'
        : atRisk === 1
          ? 'MONITORING DRIFT'
          : 'STABLE TRAJECTORY'

    return {
      pursuits,
      trajectory,
      tone: atRisk >= 2 ? 'critical' : 'ok',
    }
  }, [indexStatus, validation, profile, timers, connected, health, ambientArmed])
}

/** Status → color for pursuit chips. */
function statusClass(status: Pursuit['status']): string {
  switch (status) {
    case 'ON TRACK':
    case 'AHEAD':
      return 'text-success'
    case 'AT RISK':
      return 'text-critical'
    default:
      return 'text-muted-foreground'
  }
}

/** Bottom-right live pursuit panel adapted to Jarvis operational goals. */
export function LivePursuit() {
  const { pursuits, trajectory, tone } = usePursuits()
  const openSettings = useAppStore((s) => s.openSettings)
  const setView = useAppStore((s) => s.setView)

  /** Map a pursuit row to the surface that owns that goal. */
  function openPursuit(id: string) {
    switch (id) {
      case 'index':
        openSettings('ingestion', 'ingestion-index')
        break
      case 'vault':
        openSettings('rules', 'rules-vault')
        break
      case 'policy':
      case 'profile':
        openSettings('rules')
        break
      case 'timers':
        setView('timers')
        break
      case 'ambient':
        setView('models')
        break
      default:
        openSettings('retrieval', 'retrieval-query')
    }
  }

  return (
    <section className="panel-ops flex flex-col gap-2 p-3">
      <header className="flex items-center justify-between">
        <h2 className="font-display text-[10px] text-muted-foreground">Live Pursuit</h2>
        <span className="font-mono text-[9px] text-muted-foreground">GOALS</span>
      </header>
      <ul className="space-y-2.5">
        {pursuits.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              onClick={() => openPursuit(p.id)}
              className="w-full cursor-pointer rounded text-left transition-colors hover:bg-muted/30"
            >
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="font-display text-[10px] text-foreground/90">{p.title}</span>
                <span className={cn('font-mono text-[8px] tracking-wider', statusClass(p.status))}>
                  {p.status}
                </span>
              </div>
              <div className="h-1 overflow-hidden rounded-full bg-muted">
                <div
                  className={cn(
                    'h-full rounded-full transition-[width] duration-500',
                    p.status === 'AT RISK' ? 'bg-critical' : 'bg-primary',
                  )}
                  style={{ width: `${p.progress}%` }}
                />
              </div>
              <p className="mt-0.5 text-[10px] text-muted-foreground">{p.detail}</p>
            </button>
          </li>
        ))}
      </ul>
      <div className="mt-1 border-t border-border/50 pt-2">
        <p className="font-display text-[9px] text-muted-foreground">Predicted Trajectory</p>
        <p
          className={cn(
            'mt-0.5 font-display text-sm',
            tone === 'critical' ? 'text-critical' : 'text-success',
          )}
        >
          {trajectory}
        </p>
      </div>
    </section>
  )
}
