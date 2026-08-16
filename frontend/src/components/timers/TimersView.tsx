import { useEffect, useState } from 'react'
import { BellRing, Loader2, Plus, Trash2 } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input, Label } from '@/components/ui/controls'
import { api } from '@/lib/api/client'
import { ensurePermission, isAutostartEnabled, setAutostart } from '@/lib/notify'
import { formatClock } from '@/lib/utils'
import { useAppStore } from '@/stores/app'

const PRESETS = [
  { label: '1 min', seconds: 60 },
  { label: '5 min', seconds: 300 },
  { label: '15 min', seconds: 900 },
  { label: '30 min', seconds: 1800 },
]

/** Manage pending timers, create new ones, and toggle Tauri autostart. */
export function TimersView() {
  const { timers, refreshTimers } = useAppStore()
  const [title, setTitle] = useState('Timer')
  const [seconds, setSeconds] = useState(300)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [autostart, setAutostartState] = useState(false)

  useEffect(() => {
    void refreshTimers()
    void isAutostartEnabled().then(setAutostartState)
    void ensurePermission()
  }, [refreshTimers])

  /** Create a timer that fires after the given delay and refresh the list. */
  async function create(secondsFromNow: number) {
    setBusy(true)
    setError(null)
    try {
      await api.createTimer({
        kind: 'timer',
        title: title.trim() || 'Timer',
        body: `Jarvis timer for ${Math.round(secondsFromNow / 60)} minute(s)`,
        fire_at: null,
        seconds_from_now: secondsFromNow,
        payload: {},
      })
      await refreshTimers()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  /** Cancel a pending timer via the API and refresh the list. */
  async function cancel(id: string) {
    await api.cancelTimer(id)
    await refreshTimers()
  }

  /** Enable or disable launch-at-login through the Tauri autostart plugin. */
  async function toggleAutostart(enabled: boolean) {
    const result = await setAutostart(enabled)
    setAutostartState(result)
  }

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b border-border px-6 py-3">
        <h1 className="text-sm font-semibold">Timers</h1>
        <p className="text-xs text-muted-foreground">
          Stored on disk and caught up after sleep or a restart. When a timer
          fires, Jarvis plays a chime and speaks the title. Native OS toasts need
          the Tauri shell; a browser tab only notifies while it is open.
        </p>
      </header>

      <div className="scrollbar-thin flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-xl flex-col gap-8 px-6 py-6">
          <section className="space-y-3">
            <div>
              <Label className="text-[13px]">Title</Label>
              <Input
                className="mt-1.5"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Timer"
              />
            </div>

            <div>
              <Label className="text-[13px]">Seconds from now</Label>
              <Input
                type="number"
                min={1}
                className="mt-1.5"
                value={seconds}
                onChange={(event) => setSeconds(Number(event.target.value))}
              />
            </div>

            <div className="flex flex-wrap gap-1.5">
              {PRESETS.map((preset) => (
                <Button
                  key={preset.seconds}
                  variant="outline"
                  size="sm"
                  onClick={() => setSeconds(preset.seconds)}
                >
                  {preset.label}
                </Button>
              ))}
            </div>

            {error && <p className="text-xs text-destructive">{error}</p>}

            <Button
              onClick={() => void create(seconds)}
              disabled={busy || seconds < 1}
            >
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
              Start timer
            </Button>
          </section>

          <section>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Pending</h2>
              <Badge variant="outline">{timers.length}</Badge>
            </div>

            {timers.length === 0 ? (
              <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border px-4 py-10 text-center">
                <BellRing className="size-5 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">No pending timers.</p>
              </div>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border">
                {timers.map((job) => (
                  <li
                    key={job.id}
                    className="flex items-center gap-3 px-3 py-2.5"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{job.title}</p>
                      <p className="text-xs text-muted-foreground">
                        Fires {formatClock(job.fire_at)} · {job.kind}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => void cancel(job.id)}
                      aria-label={`Cancel ${job.title}`}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="rounded-lg border border-border p-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold">Launch at login</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  Keep the sidecar running so scheduled jobs fire without opening
                  the window. Only available inside the desktop shell.
                </p>
              </div>
              <Button
                variant={autostart ? 'secondary' : 'outline'}
                size="sm"
                onClick={() => void toggleAutostart(!autostart)}
              >
                {autostart ? 'On' : 'Off'}
              </Button>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
