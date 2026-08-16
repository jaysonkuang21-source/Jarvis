/**
 * Settings strip showing probed machine specs from GET /api/system.
 */

import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { api } from '@/lib/api/client'
import type { SystemInfo } from '@/lib/api/types'

/** Compact RAM / GPU / CPU badges for the Models settings tab. */
export function SystemSpecsBanner() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    /** Load hardware probe once when the Models tab mounts this banner. */
    void api
      .system()
      .then((info) => {
        if (!cancelled) setSystem(info)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not probe hardware')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return (
      <p className="text-xs text-muted-foreground" role="status">
        Specs unavailable: {error}
      </p>
    )
  }

  if (!system) {
    return (
      <p className="text-xs text-muted-foreground" role="status">
        Probing machine specs…
      </p>
    )
  }

  const ram =
    system.ram_available_mb != null && system.ram_total_mb != null
      ? `${Math.round(system.ram_available_mb / 1024)} / ${Math.round(system.ram_total_mb / 1024)} GB RAM free`
      : system.ram_total_mb != null
        ? `${Math.round(system.ram_total_mb / 1024)} GB RAM`
        : 'RAM unknown'

  const gpu =
    Array.isArray(system.gpus) && system.gpus.length > 0
      ? system.gpus
          .map((g) => {
            const vram =
              g.vram_free_mb != null
                ? `${Math.round(g.vram_free_mb / 1024)} GB free`
                : g.vram_total_mb != null
                  ? `${Math.round(g.vram_total_mb / 1024)} GB`
                  : ''
            return vram ? `${g.name} (${vram})` : g.name
          })
          .join(' · ')
      : 'No NVIDIA GPU detected'

  return (
    <div className="flex flex-wrap gap-1.5" data-settings-focus="system-specs">
      <Badge variant="outline">{ram}</Badge>
      <Badge variant="outline">
        {system.cpu_cores != null ? `${system.cpu_cores} CPU cores` : 'CPU unknown'}
      </Badge>
      <Badge variant="outline">{gpu}</Badge>
      {Array.isArray(system.probe_errors) && system.probe_errors.length > 0 && (
        <Badge variant="warning" title={system.probe_errors.join('; ')}>
          probe degraded
        </Badge>
      )}
    </div>
  )
}
