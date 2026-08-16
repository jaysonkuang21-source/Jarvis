import { lazy, Suspense, useEffect, useState } from 'react'
import {
  Bell,
  LayoutDashboard,
  ListTodo,
  MessageSquare,
  Moon,
  Settings,
  Sun,
  Timer,
  Wifi,
  WifiOff,
} from 'lucide-react'
import { ChatView } from '@/components/chat/ChatView'
import { CitationsPanel } from '@/components/citations/CitationsPanel'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { CommandCenter } from '@/components/hub/CommandCenter'
import { ModelsTodoPanel } from '@/components/hub/ModelsTodoPanel'
import { SettingsView } from '@/components/settings/SettingsView'
import { TimersView } from '@/components/timers/TimersView'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { bootstrapApiToken, setApiToken } from '@/lib/api/client'
import { isDemoMode } from '@/lib/demo'
import { clearSessionLlmCredentials } from '@/lib/sessionLlm'
import { useAppStore, type View } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useProfileStore } from '@/stores/profile'
import { cn } from '@/lib/utils'

const DemoAuthGate = lazy(async () => {
  const mod = await import('@/components/demo/DemoAuthGate')
  return { default: mod.DemoAuthGate }
})


const FULL_NAV: { id: View; label: string; icon: typeof MessageSquare }[] = [
  { id: 'hub', label: 'Command Center', icon: LayoutDashboard },
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'timers', label: 'Timers', icon: Timer },
  { id: 'settings', label: 'Settings', icon: Settings },
  { id: 'models', label: 'Models TODO', icon: ListTodo },
]

const DEMO_NAV: { id: View; label: string; icon: typeof MessageSquare }[] = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
]

/** Root shell: desktop hub, or chat-only gated demo. */
export default function App() {
  const { view, setView, openSettings, theme, toggleTheme, connected, health, toasts, dismissToast, init } =
    useAppStore()
  const selectedCitation = useChatStore((state) => state.selectedCitation)
  const selectCitation = useChatStore((state) => state.selectCitation)
  const loadProfile = useProfileStore((state) => state.load)

  const [authLoading, setAuthLoading] = useState(isDemoMode)
  const [signedIn, setSignedIn] = useState(!isDemoMode)
  const [email, setEmail] = useState<string | null>(null)

  useEffect(() => {
    if (!isDemoMode) return
    // Force chat-only in demo builds even if stale store state persists.
    setView('chat')
  }, [setView])

  useEffect(() => {
    if (!isDemoMode) return
    let stop = () => {}
    void import('@/lib/supabase').then(({ onAuthSession }) => {
      stop = onAuthSession((session) => {
        const token = session?.access_token ?? null
        setApiToken(token)
        setSignedIn(Boolean(session))
        setEmail(session?.user?.email ?? null)
        setAuthLoading(false)
        if (!session) {
          clearSessionLlmCredentials()
        }
        if (session) {
          void loadProfile()
        }
      })
    })
    return () => stop()
  }, [loadProfile])

  useEffect(() => {
    if (isDemoMode) return
    let cancelled = false
    let stop = () => {}
    void (async () => {
      // Token first: profile/telemetry/SSE must not race an empty Bearer header.
      await bootstrapApiToken()
      if (cancelled) return
      stop = init()
      void loadProfile()
    })()
    return () => {
      cancelled = true
      stop()
    }
  }, [init, loadProfile])

  useEffect(() => {
    if (!isDemoMode || !signedIn) return
    let cancelled = false
    let stop = () => {}
    void (async () => {
      await bootstrapApiToken()
      if (cancelled) return
      stop = init()
      void loadProfile()
    })()
    return () => {
      cancelled = true
      stop()
    }
  }, [init, loadProfile, signedIn])

  const nav = isDemoMode ? DEMO_NAV : FULL_NAV
  const showCitations =
    (view === 'chat' || view === 'hub') && selectedCitation != null

  const shell = (
    <div className="ops-grid flex h-full overflow-hidden text-foreground">
      <aside className="flex w-14 shrink-0 flex-col items-center gap-1 border-r border-border/60 bg-background/70 py-3 backdrop-blur-sm">
        <div
          className="mb-3 flex size-9 items-center justify-center rounded border border-critical/50 bg-critical/15 font-display text-xs font-bold text-critical"
          title="JARVIS"
        >
          J
        </div>

        {nav.map(({ id, label, icon: Icon }) => (
          <Tooltip key={id}>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={() =>
                  id === 'settings' ? openSettings() : setView(id)
                }
                aria-label={label}
                aria-current={view === id ? 'page' : undefined}
                className={cn(
                  'flex size-10 items-center justify-center rounded transition-colors',
                  view === id
                    ? 'bg-primary/15 text-primary'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                <Icon className="size-4.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="right">{label}</TooltipContent>
          </Tooltip>
        ))}

        <div className="flex-1" />

        {!isDemoMode && (
          <Tooltip>
            <TooltipTrigger asChild>
              <div
                className={cn(
                  'mb-1 flex size-8 items-center justify-center rounded-md',
                  connected ? 'text-success' : 'text-muted-foreground',
                )}
                aria-label={connected ? 'Backend connected' : 'Backend disconnected'}
              >
                {connected ? <Wifi className="size-3.5" /> : <WifiOff className="size-3.5" />}
              </div>
            </TooltipTrigger>
            <TooltipContent side="right">
              {connected
                ? 'Listening for timers'
                : health?.ok || health?.status === 'healthy'
                  ? 'Reconnecting to event stream'
                  : 'Backend unreachable'}
            </TooltipContent>
          </Tooltip>
        )}

        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={toggleTheme}
              aria-label="Toggle theme"
              className="flex size-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            {theme === 'dark' ? 'Light mode' : 'Dark mode'}
          </TooltipContent>
        </Tooltip>
      </aside>

      <main className="relative flex min-w-0 flex-1 flex-col bg-background/40">
        {!isDemoMode && view === 'hub' && (
          <ErrorBoundary label="Command Center" onReset={() => setView('hub')}>
            <CommandCenter />
          </ErrorBoundary>
        )}
        {view === 'chat' && (
          <ErrorBoundary label="Chat" onReset={() => setView('chat')}>
            <ChatView />
          </ErrorBoundary>
        )}
        {!isDemoMode && view === 'settings' && (
          <ErrorBoundary label="Settings" onReset={() => openSettings()}>
            <SettingsView />
          </ErrorBoundary>
        )}
        {!isDemoMode && view === 'timers' && (
          <ErrorBoundary label="Timers" onReset={() => setView('timers')}>
            <TimersView />
          </ErrorBoundary>
        )}
        {!isDemoMode && view === 'models' && (
          <ErrorBoundary label="Models TODO" onReset={() => setView('models')}>
            <ModelsTodoPanel />
          </ErrorBoundary>
        )}
      </main>

      {showCitations && selectedCitation && (
        <CitationsPanel
          citation={selectedCitation}
          onClose={() => selectCitation(null)}
        />
      )}

      {toasts.length > 0 && (
        <div className="pointer-events-none absolute bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className="panel-ops pointer-events-auto p-3 shadow-lg"
            >
              <div className="flex items-start gap-2">
                <Bell className="mt-0.5 size-4 shrink-0 text-primary" />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">
                    {toast.title}
                    {toast.missed && (
                      <span className="ml-1.5 text-xs font-normal text-warning">missed</span>
                    )}
                  </p>
                  {toast.body && (
                    <p className="mt-0.5 text-xs text-muted-foreground">{toast.body}</p>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => dismissToast(toast.id)}
                  aria-label="Dismiss"
                >
                  ×
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )

  return (
    <TooltipProvider delayDuration={250}>
      {isDemoMode ? (
        <Suspense
          fallback={
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Loading demo…
            </div>
          }
        >
          <DemoAuthGate
            email={email}
            loading={authLoading}
            signedIn={signedIn}
          >
            {shell}
          </DemoAuthGate>
        </Suspense>
      ) : (
        shell
      )}
    </TooltipProvider>
  )
}
