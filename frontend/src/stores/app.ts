import { create } from 'zustand'
import { api, bootstrapApiToken, subscribeEvents } from '@/lib/api/client'
import type { HealthResponse, MetricsResponse } from '@/lib/api/types'
import { notify, playAlertChime } from '@/lib/notify'
import { speak } from '@/lib/speech/tts'
import type { Job } from '@/lib/api/types'

export type View = 'hub' | 'chat' | 'timers' | 'settings' | 'models'

/** Settings panel tab ids (match TabsTrigger values in SettingsView). */
export type SettingsTab = 'retrieval' | 'models' | 'ingestion' | 'rules'

export interface Toast {
  id: string
  title: string
  body: string
  missed: boolean
}

interface AppState {
  view: View
  theme: 'light' | 'dark'
  health: HealthResponse | null
  metrics: MetricsResponse | null
  connected: boolean
  timers: Job[]
  toasts: Toast[]
  /** Chat drawer open over the hub (ASK JARVIS). */
  chatOpen: boolean
  /** Which SettingsView tab to show after navigating to settings. */
  settingsTab: SettingsTab
  /** Section id inside SettingsView to scroll/focus after open. */
  settingsFocus: string | null
  /** LiveSignals / hub panel id to highlight (health, metrics, …). */
  hubFocus: string | null

  setView: (view: View) => void
  setChatOpen: (open: boolean) => void
  setSettingsTab: (tab: SettingsTab) => void
  /** Clear settings deep-link focus after SettingsView consumes it. */
  clearSettingsFocus: () => void
  /** Clear hub panel highlight after LiveSignals consumes it. */
  clearHubFocus: () => void
  /** Navigate to settings and open a specific tab (optional section focus). */
  openSettings: (tab?: SettingsTab, focus?: string) => void
  /** Stay on (or return to) the hub and highlight a Live Signals row. */
  focusHubSignal: (panel: string, status?: { title: string; body: string }) => void
  /** Push a transient in-app status toast (HUD, not OS notifications). */
  pushToast: (title: string, body: string) => void
  toggleTheme: () => void
  init: () => () => void
  refreshTimers: () => Promise<void>
  refreshTelemetry: () => Promise<void>
  dismissToast: (id: string) => void
}

const THEME_KEY = 'jarvis.theme'

/** Apply theme class to the document and persist the choice in localStorage. */
function applyTheme(theme: 'light' | 'dark') {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  localStorage.setItem(THEME_KEY, theme)
}

/**
 * Resolve the initial theme. Command center defaults to dark; respect stored
 * preference when present.
 */
function initialTheme(): 'light' | 'dark' {
  const stored = localStorage.getItem(THEME_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return 'dark'
}

export const useAppStore = create<AppState>()((set, get) => ({
  view: 'hub',
  theme: 'dark',
  health: null,
  metrics: null,
  connected: false,
  timers: [],
  toasts: [],
  chatOpen: false,
  settingsTab: 'retrieval',
  settingsFocus: null,
  hubFocus: null,

  /**
   * Switch the active main view.
   * Hub-only Ask Jarvis drawer closes whenever we leave the hub.
   */
  setView: (view) =>
    set({
      view,
      chatOpen: view === 'hub' ? get().chatOpen : false,
      hubFocus: view === 'hub' ? get().hubFocus : null,
    }),

  /** Open or close the Ask Jarvis chat drawer on the hub. */
  setChatOpen: (open) => set({ chatOpen: open }),

  /** Remember which settings tab to show (used by Agent Presence deep links). */
  setSettingsTab: (tab) => set({ settingsTab: tab }),

  /** Clear settings deep-link focus after SettingsView consumes it. */
  clearSettingsFocus: () => set({ settingsFocus: null }),

  /** Clear hub panel highlight after LiveSignals consumes it. */
  clearHubFocus: () => set({ hubFocus: null }),

  /** Jump to settings, optionally selecting a tab and section focus id. */
  openSettings: (tab = 'retrieval', focus) =>
    set({
      view: 'settings',
      settingsTab: tab,
      settingsFocus: focus ?? null,
      chatOpen: false,
      hubFocus: null,
    }),

  /** Return to the hub and briefly highlight a Live Signals panel. */
  focusHubSignal: (panel, status) => {
    const id = `hub-${panel}-${Date.now()}`
    const toast: Toast | null = status
      ? {
          id,
          title: status.title,
          body: status.body,
          missed: false,
        }
      : null
    set((state) => ({
      view: 'hub',
      hubFocus: panel,
      chatOpen: false,
      toasts: toast ? [...state.toasts, toast] : state.toasts,
    }))
    if (toast) {
      window.setTimeout(() => get().dismissToast(id), 4500)
    }
  },

  /** Push a transient in-app status toast for radar / nav feedback. */
  pushToast: (title, body) => {
    const id = `status-${Date.now()}`
    set((state) => ({
      toasts: [...state.toasts, { id, title, body, missed: false }],
    }))
    window.setTimeout(() => get().dismissToast(id), 4500)
  },

  /** Toggle light/dark theme and persist it to localStorage. */
  toggleTheme: () => {
    const theme = get().theme === 'dark' ? 'light' : 'dark'
    applyTheme(theme)
    set({ theme })
  },

  /** Remove a toast by id. */
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),

  /** Reload pending timers from the backend. */
  refreshTimers: async () => {
    const timers = await api.timers().catch(() => [])
    set({ timers })
  },

  /** Refresh health + metrics for hub telemetry panels. */
  refreshTelemetry: async () => {
    const [health, metrics] = await Promise.all([
      api.health().catch(() => null),
      api.metrics().catch(() => null),
    ])
    // Health must clear on failure — never keep a stale "healthy" snapshot.
    set({
      health,
      metrics: metrics ?? get().metrics,
    })
  },

  /**
   * Bootstrap theme, then (after API token) telemetry, timers, and SSE.
   * Returns a cleanup that cancels in-flight init and closes the subscription
   * (StrictMode / HMR safe via a cancelled flag checked after awaits).
   */
  init: () => {
    const theme = initialTheme()
    applyTheme(theme)
    set({ theme })

    let cancelled = false
    let unsubscribe = () => {}
    let telemetryInterval: ReturnType<typeof setInterval> | null = null
    void (async () => {
      await bootstrapApiToken()
      if (cancelled) return
      void get().refreshTelemetry()
      void get().refreshTimers()
      telemetryInterval = setInterval(() => void get().refreshTelemetry(), 15_000)
      unsubscribe = subscribeEvents(
        (payload) => {
          const title = payload.missed ? `${payload.title} (missed)` : payload.title
          const body = payload.missed
            ? `${payload.body} Was due at ${new Date(payload.fire_at).toLocaleString()}.`
            : payload.body
          // Immediate chime + spoken announce (visual toast alone is silent).
          playAlertChime()
          const announce = payload.missed
            ? `Missed timer: ${payload.title}.`
            : `Timer finished: ${payload.title}.`
          speak(announce)
          void notify({ title, body })
          set((state) => ({
            toasts: [
              ...state.toasts,
              {
                id: payload.id,
                title: payload.title,
                body: payload.body,
                missed: payload.missed,
              },
            ],
          }))
          void get().refreshTimers()
        },
        (connected) => set({ connected }),
      )
      if (cancelled) {
        unsubscribe()
        unsubscribe = () => {}
        if (telemetryInterval) {
          clearInterval(telemetryInterval)
          telemetryInterval = null
        }
      }
    })()

    return () => {
      cancelled = true
      unsubscribe()
      if (telemetryInterval) clearInterval(telemetryInterval)
    }
  },
}))
