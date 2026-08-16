import { useAmbienceStore } from '@/stores/ambience'
import { useAppStore } from '@/stores/app'
import { cn } from '@/lib/utils'
import { AgentPresence } from './AgentPresence'
import { BottomBar } from './BottomBar'
import { ChatDrawer } from './ChatDrawer'
import { ContextualAwareness } from './ContextualAwareness'
import { CoordinationNotes } from './CoordinationNotes'
import { LivePursuit } from './LivePursuit'
import { LiveSignals } from './LiveSignals'
import { ModelsTodoStrip } from './ModelsTodoStrip'
import { RadarHub } from './RadarHub'
import { VoiceHud } from './VoiceHud'

/**
 * JARVIS operational awareness hub: radar core, telemetry columns, agent
 * presence, pursuits, ambient bottom bar, and Ask-Jarvis chat drawer.
 */
export function CommandCenter() {
  const isSpeaking = useAmbienceStore((s) => s.isSpeaking)
  const health = useAppStore((s) => s.health)
  const backendOk = health?.ok || health?.status === 'healthy'

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="pointer-events-none absolute left-1/2 top-3 z-10 -translate-x-1/2">
        <div
          className={cn(
            'panel-ops px-4 py-1.5 font-mono text-[10px] tracking-widest',
            !backendOk ? 'text-critical' : isSpeaking ? 'text-critical' : 'text-primary',
          )}
        >
          AWARENESS FIELD:{' '}
          {!backendOk
            ? 'CRITICAL RISK DETECTED'
            : isSpeaking
              ? 'CHANNEL ACTIVE — SPEAKING'
              : 'MONITORING'}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-hidden p-3 pt-12 lg:grid-cols-[240px_minmax(0,1fr)_280px]">
        {/* Left column */}
        <div className="scrollbar-thin flex min-h-0 flex-col gap-3 overflow-y-auto">
          <ContextualAwareness />
          <LiveSignals />
          <CoordinationNotes />
        </div>

        {/* Center hub */}
        <div className="relative flex min-h-0 flex-col items-center justify-center">
          <RadarHub />
          <VoiceHud />
          <p className="mt-8 max-w-sm text-center font-mono text-[9px] tracking-wider text-muted-foreground">
            HOLD CORE = VOICE AGENT · ASK JARVIS / CHAT = VAULT RAG
          </p>
        </div>

        {/* Right column */}
        <div className="scrollbar-thin relative z-10 flex min-h-0 flex-col gap-3 overflow-y-auto">
          <AgentPresence />
          <LivePursuit />
          <ModelsTodoStrip />
        </div>
      </div>

      <BottomBar />
      <ChatDrawer />
    </div>
  )
}
